/** MQTTX Script Function. Select Publish, then enable timed messages every 5 seconds. */
// One identifier per script session; this is a sample identity, not a credential.
const bootId = Array.from({length:16}, () => Math.floor(Math.random()*256).toString(16).padStart(2,'0')).join('');
function handlePayload(value, msgType, index) {
  if (msgType !== 'publish') return value;
  const sequence = Number.isInteger(index) && index >= 0 ? index + 1 : 1;
  return {
    device_id: 'cabinet-01',
    boot_id: bootId,
    seq: sequence,
    temperature_c: Math.round((24 + 1.2 * Math.sin(sequence / 3)) * 100) / 100,
    humidity_pct: Math.round((48 + 7 * Math.cos(sequence / 4)) * 100) / 100
  };
}

execute(handlePayload);
