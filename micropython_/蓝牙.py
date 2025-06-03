import bluetooth
import struct

class BLEService():
    def __init__(self):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        if not self.ble.active():
            raise RuntimeError("无法激活BLE")

        self.ble.config(gap_name="Seizure-3")
        self.ble.irq(self._irq_callback)

        self.srv_handle = None
        self.chr_handle = None
        self.connected = False

        self._setup_service()   # 注册服务
        
        self._start_advertising()

    def _setup_service(self):
        SERVICE_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        CHAR_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
        
        services = self.ble.gatts_register_services([(
            SERVICE_UUID,
            ((CHAR_UUID, bluetooth.FLAG_READ | bluetooth.FLAG_WRITE),)
        )])
        print("服务注册返回值:", services)
        self.char_handle = services[0][0]
        self.ble.gatts_write(services[0][0],bytes(4096))  # 设置接收数据大小

    def _start_advertising(self):
        adv_data = bytearray()
        adv_data += b'\x02\x01\x06'
        adv_data += b'\x03\x03' + struct.pack("<H", 0x180F)  # 示例服务UUID (16-bit format)
        adv_data += b'\x0A\x09' + "Seizure-3".encode('utf-8')
        self.ble.gap_advertise(100, adv_data)

    def _irq_callback(self, event, data):
        if event == 1: 
            print("[Bluetooth - Connected]")
            self.connected = True
        elif event == 2:  
            print("[Bluetooth - Disconnected]")
            self.connected = False
            self._start_advertising()
        elif event == 3:  
            conn_handle, attr_handle = data
            received_data = self.ble.gatts_read(attr_handle)
            print(f"接收到的数据大小为：{len(received_data)} 字节")
            try:
                float_count = len(received_data) // 4
                floats = struct.unpack('<' + 'f' * float_count, received_data)
                print(f"接收到的浮点数: {floats}")
            except Exception as e:
                print(f"数据解析错误: {e}")

def main():
     ble_service = BLEService()
     
if __name__ == "__main__":
    main()