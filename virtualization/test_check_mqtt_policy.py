import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_mqtt_policy import classify, script
from check_matrix import encoded_python


class MQTTPolicyTests(unittest.TestCase):
    def test_explicit_reason_codes_and_delivery_required(self):
        for mode in ('anonymous','wrong_password'):
            self.assertTrue(classify({'mode':mode,'source_ip':'192.168.20.100','connack':135}))
            self.assertFalse(classify({'mode':mode,'source_ip':'192.168.20.100','connack':0}))
        self.assertTrue(classify({'mode':'other_topic','source_ip':'192.168.20.100','connack':0,'puback':135}))
        row={'mode':'device_read','source_ip':'192.168.20.100','connack':0,'suback':[1],'probe_puback':0,'delivered':0}
        self.assertTrue(classify(row));row['delivered']=1;self.assertFalse(classify(row))

    def test_commands_compile_within_real_serial_limit(self):
        for mode in ('anonymous','wrong_password','other_topic','device_read'):
            source=script(mode,'WH_MQTT_'+mode+'_');compile(source,'<mqtt>','exec')
            self.assertLess(len(encoded_python(source).encode()),3000)


if __name__=='__main__':unittest.main()
