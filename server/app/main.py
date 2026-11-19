"""Read-only HTTP diagnosis, single MQTT ingestion and background incidents."""
import csv
import io
import logging
import os
import sqlite3

from flask import Flask,Response,jsonify,render_template,request,stream_with_context
import paho.mqtt.client as mqtt
from waitress import serve

from .config import load
from .observations import ObservationReader
from .state import DEVICE_ID,TOPIC,SCHEMA_VERSION,TelemetryStore,iso_timestamp

LOG=logging.getLogger('warehouse')


def version():
    return {'version':os.environ.get('APP_VERSION','dev'),
            'git_commit':os.environ.get('GIT_COMMIT','unknown'),
            'source_sha256':os.environ.get('SOURCE_SHA256','unknown'),
            'schema_version':SCHEMA_VERSION}


def create_app(store,observation_reader=None):
    app=Flask(__name__)
    observations=observation_reader or ObservationReader(os.environ.get('OBSERVATIONS_DIR'),clock=store.clock)
    demo_mode=os.environ.get('DEMO_MODE','false').lower() in ('true','1')

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        return response

    @app.errorhandler(sqlite3.Error)
    def database_unavailable(error):
        LOG.error('Read query unavailable (%s)',type(error).__name__)
        return jsonify(error='storage_unavailable',error_type=type(error).__name__),503

    def integer(name,default,lower,upper):
        try:
            value=int(request.args.get(name,str(default)))
        except (ValueError,TypeError):
            raise ValueError(f'{name} must be an integer from {lower} to {upper}') from None
        if not lower<=value<=upper:
            raise ValueError(f'{name} must be an integer from {lower} to {upper}')
        return value

    @app.errorhandler(ValueError)
    def bad_request(error):
        return jsonify(error=str(error)),400

    @app.get('/')
    def index():
        return render_template('index.html',device_id=DEVICE_ID,demo_mode=demo_mode)

    @app.get('/api/status')
    def status():
        return jsonify(**store.status(),demo_mode=demo_mode)

    @app.get('/api/version')
    def app_version():
        return jsonify(version())

    @app.get('/api/health')
    def health():
        current=store.status()
        observed=current['observed_at']
        components={
            'broker':{'state':'healthy' if current['broker_connected'] else 'unhealthy',
                'source':'service','observed_at':observed,'evidence':{'subscription_active':current['broker_connected']}},
            'telemetry':{'state':'healthy' if current['data_status']=='online' else 'unknown' if current['data_status']=='awaiting_data' else 'unhealthy',
                'source':'service','observed_at':observed,'evidence':{'data_status':current['data_status'],
                    'age_seconds':current['age_seconds'],'last_seen_at':current['last_seen_at']}},
            'storage':{'state':current['storage_state'],'source':'service','observed_at':observed,
                'evidence':{'last_success_at':current['last_persist_success_at'],'last_error_at':current['last_persist_error_at'],
                            'last_error':current['last_persist_error'],'persist_errors':current['counters']['persist_errors']}}
        }
        components.update(observations.read())
        states={row['state'] for row in components.values()}
        state='unhealthy' if 'unhealthy' in states else 'unknown' if 'unknown' in states else 'healthy'
        return jsonify(observed_at=observed,state=state,components=components,
                       **store.live_events(),version=version())

    @app.get('/api/events')
    def events():
        limit=integer('limit',50,1,200)
        after=integer('after_id',0,0,2**63-1) if 'after_id' in request.args else None
        return jsonify(store.events(after,limit))

    @app.get('/api/event-changes')
    def event_changes():
        return jsonify(store.event_changes(integer('after_id',0,0,2**63-1),integer('limit',200,1,200)))

    @app.get('/api/history')
    def history():
        return jsonify(device_id=DEVICE_ID,readings=store.history(integer('limit',120,1,240)))

    @app.get('/api/history/range')
    def history_range():
        return jsonify(store.history_range(request.args.get('start'),request.args.get('end'),request.args.get('resolution','auto')))

    @app.get('/api/history/export.csv')
    def export_csv():
        connection,cursor=store.open_export(request.args.get('start'),request.args.get('end'))
        def generate():
            output=io.StringIO(newline='')
            writer=csv.writer(output)
            def line(values):
                output.seek(0)
                output.truncate()
                writer.writerow(values)
                return output.getvalue()
            try:
                yield line(('device_id','boot_id','seq','temperature_c','humidity_pct','received_at'))
                while True:
                    rows=cursor.fetchmany(500)
                    if not rows: break
                    for device,boot,seq,temperature,humidity,received in rows:
                        yield line((device or DEVICE_ID,boot or '',seq,temperature,
                                    humidity if humidity is not None else '',iso_timestamp(received)))
            finally:
                connection.close()
        return Response(stream_with_context(generate()),mimetype='text/csv',
            headers={'Content-Disposition':'attachment; filename="warehouse-readings.csv"'})

    @app.get('/healthz')
    def healthz():
        # Liveness does not read storage; a failed volume must leave diagnosis up.
        return jsonify(ok=True,broker_connected=store.broker_connected)

    return app


def make_client(store):
    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='warehouse-dashboard',clean_session=True)
    client.username_pw_set(os.environ.get('MQTT_USERNAME','warehouse-dashboard'),os.environ['MQTT_PASSWORD'])

    def on_connect(connection,userdata,flags,reason_code,properties):
        if reason_code.is_failure:
            store.set_broker_connected(False)
            LOG.warning('MQTT connection rejected: %s',reason_code)
            return
        result,_=connection.subscribe(TOPIC,qos=1)
        if result!=mqtt.MQTT_ERR_SUCCESS: store.set_broker_connected(False)

    def on_subscribe(connection,userdata,mid,reason_codes,properties):
        accepted=bool(reason_codes) and all(not result.is_failure for result in reason_codes)
        store.set_broker_connected(accepted)
        LOG.info('MQTT subscription active=%s',accepted)

    def on_disconnect(connection,userdata,flags,reason_code,properties):
        store.set_broker_connected(False)
        LOG.warning('MQTT disconnected: %s',reason_code)

    def on_message(connection,userdata,message):
        if message.topic!=TOPIC: return
        try: store.ingest(message.payload,retained=message.retain)
        except ValueError as error: LOG.warning('Ignored telemetry: %s',error)
        except Exception: LOG.exception('Telemetry persistence failed')

    client.on_connect=on_connect
    client.on_subscribe=on_subscribe
    client.on_disconnect=on_disconnect
    client.on_message=on_message
    client.reconnect_delay_set(min_delay=1,max_delay=8)
    return client


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    demo_mode=os.environ.get('DEMO_MODE','false').lower() in ('true','1')
    config=load(os.environ.get('WAREHOUSE_CONFIG_FILE'),demo_mode)
    store=TelemetryStore(os.environ.get('SQLITE_PATH','/data/warehouse.sqlite3'),
        int(os.environ.get('OFFLINE_AFTER_SECONDS','20')),config=config)
    client=make_client(store)
    client.connect_async(os.environ.get('MQTT_HOST','broker'),int(os.environ.get('MQTT_PORT','1883')),keepalive=15)
    client.loop_start()
    store.start_background()
    try: serve(create_app(store),host='0.0.0.0',port=8080,threads=4)
    finally:
        client.disconnect()
        client.loop_stop()
        store.close()


if __name__=='__main__':
    main()
