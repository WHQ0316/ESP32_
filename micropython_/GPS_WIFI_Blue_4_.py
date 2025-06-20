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
import struct
from bluetooth import UUID, FLAG_READ, FLAG_WRITE
import ustruct
from machine import UART, Pin
import neopixel

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

# 服务器配置
URL_web = "http://10.120.87.109:5000/api/device/data"      # 服务器地址：电脑端     
# URL_web = "http://8.154.30.107/api/device/data"              # 服务器地址：阿里云


# BLE数据缓冲

# 采样率200HZ
ble_data_received = False  # BLE数据接收完成标志

# ============================ 数据池（蓝牙数据） ============================
# 数据池：存放蓝牙接收到的数据（固定长度，循环队列）
class data_pool():
    def __init__(self, size):
        self.size = size
        self.buffer = [None] * size
        self.index = 0

    def append(self, item):
        """向缓冲区添加新项目"""
        self.buffer[self.index] = item
        self.index = (self.index + 1) % self.size

    def get_new(self, n=4):
        """获取最新的n个项目"""
        if self.index >= n:
            return self.buffer[self.index-n:self.index]
        else:
            return None

buffer_size = 10  # 定义缓冲区大小
pool_data = data_pool(buffer_size)
# ============================ BLE模块（蓝牙） ============================
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
        # BLE配置
        SERVICE_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        CHAR_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
        
        services = self.ble.gatts_register_services([(
            SERVICE_UUID,
            ((CHAR_UUID, bluetooth.FLAG_READ | bluetooth.FLAG_WRITE),)
        )])
        print("服务注册返回值:", services)
        self.char_handle = services[0][0]
        self.ble.gatts_write(services[0][0],bytes(4096))  # 设置接收数据大小  bytearray

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
                # print(f"接收到的浮点数: {floats}")
                # 将数据存入数据池中等待POST取
                pool_data.append(floats)
            except Exception as e:
                print(f"数据解析错误: {e}")


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
        self.last_gps_time = 0  # 新增时间记录变量

    def read_gps_data(self):
        """读取并解析GPS数据"""
        if time.time() - self.last_gps_time >= 2.0:   # 定位每两秒读一次，给出时间处理其他事件
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
        """优化后的GLL语句解析方法"""
        try:
            parts = sentence.split(',')
            if len(parts) < 7:  # GLL语句至少需要7个字段
                return False
            
            global gps_data_valid, nmea_x, nmea_y, last_x, last_y
            
            # 快速检查数据有效性
            if parts[6] != 'A':  # 状态不是'A'ctive
                print(f"[Invalid GPS] Status: {parts[6]}")
                if last_x != 0:  # 有历史数据可用
                    nmea_x = last_x
                    nmea_y = last_y
                    gps_data_valid = True
                    print("[Using historical data]")
                return False

            # 解析纬度
            lat = float(parts[1])
            if 'S' in parts[2]:
                lat = -lat
                
            # 解析经度
            lon = float(parts[3])
            if 'W' in parts[4]:
                lon = -lon
                
            # 更新当前数据
            self.nmea_x = lon
            self.nmea_y = lat
            nmea_x = self.nmea_x
            nmea_y = self.nmea_y
            
            # 更新历史数据
            last_x = nmea_x
            last_y = nmea_y
            gps_data_valid = True
             # if __debug__:
                # print(f"[Valid GPS] Lat: {self.nmea_y:.6f}, Lon: {self.nmea_x:.6f}")
            
            gps_data_valid = True
            self.last_gps_time = time.time()  # 记录最后有效GPS时间
            return True
            
        except ValueError as ve:
            print(f"坐标转换错误: {ve}")
            return False
        except Exception as e:
            print(f"解析GLL异常: {e}")
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
    # 缓存上一次的时间字符串，避免重复生成
    _last_time_str = ""
    _last_time_sec = 0
    
    @staticmethod
    def _get_current_time_str():
        """优化时间字符串生成"""
        now = time.time()
        if now - DataUploader._last_time_sec < 2.0:  # 2秒内使用缓存
            return DataUploader._last_time_str
        
        current_time = time.localtime()
        DataUploader._last_time_str = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*current_time[:6])
        DataUploader._last_time_sec = now
        return DataUploader._last_time_str

    @staticmethod
    def generate_payload():
        """生成上传数据负载 - 优化版"""
        try:
            # 预检查数据有效性
            if pool_data.get_new() == None:
                return None

            # 使用优化的时间获取方法
            time_str = DataUploader._get_current_time_str()

            # 构建最小化payload
            payload = {
                'device_id': device_id,  # 假设device_id已经是字符串
                'position_x': "%.6f" % nmea_x,  # 限制小数位数
                'position_y': "%.6f" % nmea_y,
                'time_stamp': time_str,
                'user_data': pool_data.get_new()  
            }

            # 调试信息改为条件输出
            if __debug__:
                try:
                    json.dumps(payload)  # 快速验证可序列化
                    print("[Payload ready] Size:", len(str(payload)))
                except Exception as je:
                    print("⚠️ 序列化错误:", je)
                    return None

            return payload

        except Exception as e:
            print(f"生成payload错误: {e}")
            return None
    
    @staticmethod
    def upload_data():
        """更健壮的上传方法"""
        try:
            payload = DataUploader.generate_payload()
            if not payload or not payload.get('user_data'):
                print("无有效数据可上传")
                return False
            
            # 确保WiFi连接
            if not wifi.sta_if.isconnected():
                if not wifi.connect():
                    return False
                    
            # 执行上传
            start_time = time.ticks_ms()
            success = http_post(URL_web, payload)
            
            if success:
                print(f"上传成功! 耗时: {time.ticks_diff(time.ticks_ms(), start_time)}ms")
                rgb_flash((0, 255, 255))
                return True
            return False
        except Exception as e:
            print(f"上传异常: {e}")
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
    print("[Start the main loop]...")
    
    # 上传时间控制变量
    last_upload_time = 0
    min_upload_interval = 0.5  # 最小上传间隔(秒)
    max_upload_interval = 3.0  # 最大上传间隔(秒)
    current_upload_interval = min_upload_interval
    
    # 状态计数器
    consecutive_failures = 0
    max_consecutive_failures = 3
    
    while True:
        try:
            # 1. 读取GPS数据
            gps_reader.read_gps_data()
            
            # 2. 检查是否应该上传
            current_time = time.time()
            if current_time - last_upload_time >= current_upload_interval:
                if wifi.sta_if.isconnected():
                    # 3. 尝试上传数据
                    if DataUploader.upload_data():
                        # 上传成功 - 重置间隔和失败计数器
                        current_upload_interval = min_upload_interval
                        consecutive_failures = 0
                        last_upload_time = current_time
                    else:
                        # 上传失败 - 增加间隔和失败计数器
                        consecutive_failures += 1
                        if consecutive_failures >= max_consecutive_failures:
                            current_upload_interval = min(
                                current_upload_interval * 2, 
                                max_upload_interval
                            )
                            consecutive_failures = 0  # 重置计数器
                            # print(f"调整上传间隔为: {current_upload_interval}秒")
                else:
                    print("WiFi未连接，尝试重新连接...")
                    if wifi.connect():
                        print("WiFi重新连接成功")
                    else:
                        print("WiFi重新连接失败")
                        time.sleep(5)  # 等待一段时间再重试
            
            # 4. 短暂休眠以节省资源
            time.sleep(0.1)
            
        except Exception as e:
            print(f"主循环错误: {e}")
            time.sleep(1)  # 出错后等待1秒再继续
#-----------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户终止程序")
    except Exception as e:
        print(f"\n程序错误: {e}")
    finally:
        print("程序结束")


