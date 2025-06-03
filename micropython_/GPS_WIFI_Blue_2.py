"""
            ESP32-S3 GPS/BLE/WiFi 数据采集传输系统
                功能：
                    1. 通过UART读取GPS模块的GLL数据
                    2. BLE从机服务接收数据
                    3. WiFi连接并发送JSON格式数据到服务器
                    4. 使用板载RGB灯指示BLE和WiFi状态
                    5. 增强调试信息与容错处理
"""

# ============================ 导入依赖 ============================
import sys  # 用于打印异常
import machine
import time
import network
import urequests
import json
import bluetooth
from bluetooth import UUID, FLAG_READ, FLAG_WRITE
import ustruct
from machine import UART, Pin
import neopixel
from ubluetooth import BLE, UUID, FLAG_READ, FLAG_WRITE

# ============================ RGB 灯配置 ============================
RGB_PIN = 48  # ESP32-S3 板载 RGB 控制引脚
RGB_NUM = 1   # RGB灯数量
np = neopixel.NeoPixel(Pin(RGB_PIN), RGB_NUM)

def rgb_flash(color=(0, 0, 255), times=2, delay_ms=200):
    """RGB灯闪烁指定颜色若干次"""
    for _ in range(times):
        np[0] = color
        np.write()
        time.sleep_ms(delay_ms)
        np[0] = (0, 0, 0)
        np.write()
        time.sleep_ms(delay_ms)

# ============================ 全局变量 ============================
device_id = "3"  # 设备编号
nmea_x = 0.0     # GPS经度
nmea_y = 0.0     # GPS纬度
gps_data_valid = False  # GPS数据有效标志
last_x = 0.0
last_y = 0.0

# GPS UART配置
GPS_UART_PORT = 1
GPS_UART_RX_PIN = 18
GPS_UART_TX_PIN = 17
GPS_UART_BUFFER_SIZE = 1024

# WiFi配置
# WIFI_SSID = "Redmi"   # "mw-OpenWrt"    
# WIFI_PASS = "wwwwwwww"   # "1176224694"
WIFI_SSID = "mw-OpenWrt"    
WIFI_PASS = "1176224694"
# URL_web = "http://10.120.87.109:5000/api/device/data"      # 服务器地址：电脑端     
URL_web = "http://8.154.30.107/api/device/data"              # 服务器地址：阿里云

# BLE配置
BLE_DEVICE_NAME = "Seizure-3"
SERVICE_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
CHAR_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')

# BLE数据缓冲
user_data = []
list_data = []
list_len = 512
ble_data_received = False  # BLE数据接收完成标志


# ============================ BLE模块（蓝牙） ============================
class BLEService:
    def __init__(self):
        """初始化BLE服务"""
        self.ble = bluetooth.BLE()
        self.ble.active(True)

        if not self.ble.active():
            raise RuntimeError("无法激活BLE")

        self.ble.config(gap_name=BLE_DEVICE_NAME)
        self.ble.irq(self._irq_callback)

        self.srv_handle = None
        self.chr_handle = None
        self.connected = False  # 连接状态标志

        self._setup_service()
        self._start_advertising()
        
        self.data_len = 0

    def _setup_service(self):
        """注册BLE服务和特征"""
        try:
            # 使用你自己的服务UUID和特征UUID
            SERVICE_UUID = bluetooth.UUID(0x1234)  # 示例服务UUID
            CHAR_UUID = bluetooth.UUID(0x5678)     # 示例特征UUID

            services = self.ble.gatts_register_services([(
                UUID(SERVICE_UUID),
                ((UUID(CHAR_UUID), FLAG_READ | FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),)
            )])
            # print("服务注册返回值:", services)

            if services and len(services) > 0:
                # 提取特征句柄
                self.char_handle = services[0][0]  # 第一个特征句柄
                # print(f"特征句柄: {self.char_handle}")
                # Added: 设置特征值最大长度（单位：字节）
                self.ble.gatts_set_buffer(self.char_handle, 512, True)
            else:
                raise RuntimeError("服务注册失败")

        except Exception as e:
            print(f"BLE服务设置错误: {e}")
            raise

    def _start_advertising(self):
        try:
            """开始BLE广播"""
            print("[Start: Bluetooth - Radio]...")
            adv_data = bytearray()
            adv_data += b'\x02\x01\x06'  # 可连接、通用广播标志
            adv_data += b'\x03\x02' + ustruct.pack("<H", 0x1234)  # 注册服务 UUID (0x1234)
            adv_data += b'\x0A\x09' + BLE_DEVICE_NAME.encode('utf-8')  # 设备名称
            adv_data += b'\x03\x1A' + ustruct.pack("<H", 517)  # 请求MTU=517

            self.ble.gap_advertise(100, adv_data)
        except OSError as e:
            if e.args[0] == -30:  # 如果遇到资源不可用错误
                print("资源暂时不可用，稍后重试...")
                time.sleep(1)  # 等待一段时间
                self._start_advertising()  # 尝试重启广播
                
    def _irq_callback(self, event, data):
        """BLE事件回调"""
        global user_data, ble_data_received, list_data, list_len

        if event == 1:  # BLE已连接
            print("[Bluetooth - Connected]")
            self.connected = True
        elif event == 2:  # BLE断开
            print("[Bluetooth - disconnected]")
            self.connected = False
            self._start_advertising()
        elif event == 3:  # 有数据写入
            # print("BLE写入事件触发")

            try:
                conn_handle, value_handle = data
                received_data = self.ble.gatts_read(value_handle)  # 直接使用 value_handle

                if received_data:
                    # print(f"原始字节数据: {received_data}")
                    try:
                        if self.data_len == list_len:
                            user_data = list_data.copy()
                            ble_data_received = True
                            list_data.clear()
                            self.data_len = 0
                            print(user_data)
                            
                        else:
                            data_str = received_data.decode('ascii')
                            # print(f"[Data by Bluetooth]: {data_str}")  # ASCII解码
                            list_data.append(data_str)
                            self.data_len += 1
                            
                    except UnicodeDecodeError:
                        print("警告：数据不是有效的ASCII字符串")
                else:
                    print("⚠️ 收到空数据")

            except Exception as e:
                print(f"❌ 处理BLE数据时出错: {e}")


# ============================ GPS模块 ============================
class GPSReader:
    def __init__(self):
        """初始化GPS UART接口"""
        self.uart = UART(
            GPS_UART_PORT, 
            baudrate=9600,
            rx=GPS_UART_RX_PIN,
            tx=GPS_UART_TX_PIN
        )
        print("[GPS UART Init Finished]")
        self.nmea_x = 0.0
        self.nmea_y = 0.0

    def read_gps_data(self):
        """读取并解析GPS数据"""
        if self.uart.any():
            try:
                raw_data = self.uart.read(GPS_UART_BUFFER_SIZE)
                nmea_sentences = raw_data.decode('utf-8').split('\r\n')
                for sentence in nmea_sentences:
                    if sentence and 'GLL' in sentence:
                        if self._parse_gll(sentence):
                            break
            except Exception as e:
                print(f"GPS数据解析错误: {e}")

    def _parse_gll(self, sentence):
        """解析GLL语句"""
        parts = sentence.split(',')
        if len(parts) >= 6:
            global gps_data_valid, nmea_x, nmea_y, last_x, last_y
            if parts[6] == 'A':
                try:
                    lat = float(parts[1])
                    lon = float(parts[3])
                    if 'S' in parts[2]:
                        lat = -lat
                    if 'W' in parts[4]:
                        lon = -lon
                    self.nmea_y = lat
                    self.nmea_x = lon
                    
                    nmea_x = self.nmea_x
                    nmea_y = self.nmea_y
                    print(f"[Valid GPS data] : Lat: {self.nmea_y}, Lon: {self.nmea_x}")
                    last_x = nmea_x   # 保留上次有效数据
                    last_y = nmea_y
                    gps_data_valid = True
                    
                    return True
                except ValueError:
                    print("坐标转换失败")
            else:
                print(f"[Invalid GPS data]: (GPS status): {parts[6]}")
                if last_x == 0:
                    print('[No historical GPS]')
                else :
                    print(f"[USE Historical GPS data] - Lat: {last_y}, Lon: {last_x}")
                    nmea_x = last_x
                    nmea_y = last_y
                    gps_data_valid = True
                    
                
        else:
            print("[Invalid GPS data]: 字段不完整")
        return False

# ============================ WiFi模块 ============================
class WiFiManager:
    def __init__(self):
        """初始化WiFi接口"""
        self.sta_if = network.WLAN(network.STA_IF)

    def connect(self):
        """连接WiFi网络"""
        if not self.sta_if.isconnected():
            print("[Connecting WiFi]...")
            self.sta_if.active(True)
            self.sta_if.connect(WIFI_SSID, WIFI_PASS)
            for _ in range(10):
                if self.sta_if.isconnected():
                    break
                time.sleep(1)
        if self.sta_if.isconnected():
            print("[WiFi connect successful]", self.sta_if.ifconfig())
            rgb_flash((0, 255, 0))
            return True
        else:
            print("[WiFi connect failed]")
            return False

# ============================ 数据上传模块 ============================
def http_post(url, data):
    """发送 HTTP POST 请求"""
    try:
        json_payload = json.dumps(data)

        print(f"[POSR URL]: {url}")
        print(f"[Request content]: {json_payload}")

        headers = {'Content-Type': 'application/json'}
        response = urequests.post(url, headers=headers, data=json_payload)

        print(f"[Response status]: {response.status_code}")
        print(f"[Respond content]: {response.text}")

        response.close()
        return True
    except Exception as e:
        print(f"上传失败: {e}")
        return False

def ensure_json_serializable(data):
    """确保数据是JSON可序列化的"""
    if isinstance(data, list):
        return [ensure_json_serializable(item) for item in data]
    elif isinstance(data, dict):
        return {str(k): ensure_json_serializable(v) for k, v in data.items()}
    elif isinstance(data, (int, float, str, bool, type(None))):
        return data
    else:
        # 如果不是基本类型，则尝试转换为字符串
        return str(data)


# ==============================================================================
# 上传数据到服务器
class DataUploader:
    @staticmethod
    def generate_payload():
        try:
            """ 调试代码
            print("[Generate data]")
            print("---------------------------------")
            print(f"device_id: {device_id}")
            print(f"nmea_x: {nmea_x}")
            print(f"nmea_y: {nmea_y}")
            print(f"user_data: {user_data}")
            print("---------------------------------")
            """
            # 确保 user_data 是 list 或 dict 类型，并且内容可序列化
            if not isinstance(user_data, (list, dict)):
                user_data_copy = ''
            else:
                user_data_copy = ensure_json_serializable(user_data.copy())

            current_time = time.localtime()
            time_str = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*current_time[:6])

            payload = {
                'device_id': str(device_id),  # 强制转换为字符串
                'position_x': str(nmea_x),
                'position_y': str(nmea_y),
                'time_stamp': time_str,
                'user_data': user_data
            }

            # 打印 payload 调试信息前，先尝试 json.dumps()
            try:
                json_string = json.dumps(payload)
                print("[prepare Uploaded data]:")
                print(json_string)
            except Exception as je:
                print("⚠️ payload 包含不可序列化的数据!")
                print(f"Payload 内容: {payload}")
                raise je

            return payload

        except Exception as e:
            print(f"生成 payload 出错: {e}")
            sys.print_exception(e)
            return None
    
    @staticmethod
    def upload_data():
        """上传数据到服务器"""
        global gps_data_valid, ble_data_received, URL_web
        
        print(f"[gps_data_valid: {gps_data_valid}, ble_data_received: {ble_data_received}]")
        
        if not gps_data_valid or not ble_data_received:
            print("[Data lack!]")
            return False
        
        payload = DataUploader.generate_payload()
        if payload is None:
            print("无法生成有效 payload")
            return False
        
        url = URL_web
        
        try:
            print(f"[Uploading data]... URL: {url}")
            
            success = http_post(url, payload)
            
            if success:
                rgb_flash((0, 255, 255))  # 假设 rgb_flash 是一个定义好的函数
                gps_data_valid = False     # 成功后再次接受数据
                ble_data_received = False  
            
            return success
        except Exception as e:
            print(f"上传失败: {e}")
            return False


# ============================ 主程序 ============================
def main():
    """主程序入口"""
    print("\n===== Seizure Detect System=====")
    print("[Init BLE Service]...")
    ble_service = BLEService()
    print("[Init GPS module]...")
    gps_reader = GPSReader()
    print("[Connect WiFi]...")
    wifi = WiFiManager()
    if not wifi.connect():
        print("警告: WiFi连接失败，将继续运行但无法上传数据")
    print("[Enter the main loop]...")
    last_upload_time = 0
    upload_interval = 2
    while True:
        gps_reader.read_gps_data()
        current_time = time.time()
        if current_time - last_upload_time >= upload_interval:
            if wifi.sta_if.isconnected():
                DataUploader.upload_data()
                last_upload_time = current_time
            else:
                print("WiFi未连接，跳过上传")
        time.sleep(1)
#-----------------------------------------------------------------------
"""
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户终止程序")
    except Exception as e:
        print(f"\n程序错误: {e}")
    finally:
        print("程序结束")

"""

