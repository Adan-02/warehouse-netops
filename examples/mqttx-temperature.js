/** MQTTX Script Function. Select Publish, then enable timed messages every 5 seconds. */
function handlePayload(value, msgType, index) {
  if (msgType !== 'publish') return value;
  const sequence = Number.isInteger(index) && index >= 0 ? index + 1 : 1;
  return {
    device_id: 'cabinet-01',
    seq: sequence,
    temperature_c: Math.round((24 + 1.2 * Math.sin(sequence / 3)) * 100) / 100,
    humidity_pct: Math.round((48 + 7 * Math.cos(sequence / 4)) * 100) / 100
  };
}

execute(handlePayload);
