# ============================ 导入依赖 ============================
import sys
import machine
import time
import json
import bluetooth
from bluetooth import UUID, FLAG_READ, FLAG_WRITE
import ustruct
from machine import UART, Pin
import neopixel
from ubluetooth import BLE

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
user_data = ''          # BLE接收数据
ble_data_received = False  # BLE数据接收完成标志

# GPS UART配置
GPS_UART_PORT = 1
GPS_UART_RX_PIN = 18
GPS_UART_TX_PIN = 17
GPS_UART_BUFFER_SIZE = 1024

# 4G模块配置
LTE_UART_PORT = 2
LTE_UART_RX_PIN = 16
LTE_UART_TX_PIN = 17
LTE_BAUDRATE = 115200

# BLE配置
BLE_DEVICE_NAME = "Seizure-3"
SERVICE_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
CHAR_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')

# 服务器配置
SERVER_URL = "http://httpbin.org/post"
SERVER_PORT = 80

# ============================ 4G模块 ============================
class LTE4GModule:
    def __init__(self):
        """初始化4G模块"""
        self.uart = UART(
            LTE_UART_PORT,
            baudrate=LTE_BAUDRATE,
            rx=LTE_UART_RX_PIN,
            tx=LTE_UART_TX_PIN,
            timeout=1000
        )
        self.http_service_active = False
        self.connected = False
        
    def send_at_command(self, command, expected_response="OK", timeout=2):
        """发送AT命令并检查响应"""
        print(f"[4G] 发送: {command.strip()}")
        self.uart.write(command + "\r\n")
        
        start_time = time.time()
        response = ""
        
        while time.time() - start_time < timeout:
            if self.uart.any():
                response += self.uart.read(self.uart.any()).decode('utf-8')
                if expected_response in response:
                    print(f"[4G] 响应: {response.strip()}")
                    return True
        
        print(f"[4G] 超时或未收到预期响应: {response.strip()}")
        return False
    
    def connect(self):
        """初始化4G连接"""
        print("[4G] 初始化4G模块...")
        
        # 1. 检查模块响应
        if not self.send_at_command("AT"):
            print("[4G] 模块无响应")
            return False
        
        # 2. 配置APN (使用默认APN)
        if not self.send_at_command('AT+QICSGP=1,1,"","",""'):
            print("[4G] APN配置失败")
            return False
        
        # 3. 激活移动场景
        if not self.send_at_command("AT+QIACT=1"):
            print("[4G] 激活移动场景失败")
            return False
        
        self.connected = True
        rgb_flash((0, 255, 0))  # 绿色表示连接成功
        return True
    
    def setup_http(self):
        """配置HTTP服务"""
        # 1. 开启HTTP服务
        if not self.send_at_command("ATSHTTPSERVE=1"):
            return False
        self.http_service_active = True
        
        # 2. 配置URL信息
        if not self.send_at_command(f'ATSHTTPPARA="URL","{SERVER_URL}"'):
            return False
        
        # 3. 配置端口
        if not self.send_at_command(f'ATSHTTPPARA="PORT",{SERVER_PORT}'):
            return False
        
        return True
    
    def set_content_length(self, length):
        """设置Content-Length请求头"""
        return self.send_at_command(f'ATSHTTPROH="Content-Length",{length}')
    
    def set_connection_header(self):
        """设置Connection请求头"""
        return self.send_at_command('ATSHTTPROH="Connection","keep-alive"')
    
    def send_post_request(self, data):
        """发送POST请求"""
        try:
            # 确保连接正常
            if not self.connected:
                if not self.connect():
                    raise Exception("4G连接失败")
            
            # 配置HTTP服务
            if not self.setup_http():
                raise Exception("HTTP服务配置失败")
            
            # 准备数据
            json_data = json.dumps(data)
            content_length = len(json_data)
            
            # 设置请求头
            if not self.set_content_length(content_length):
                raise Exception("设置Content-Length失败")
            
            if not self.set_connection_header():
                raise Exception("设置Connection头失败")
            
            # 启动HTTP动作 (1表示POST)
            if not self.send_at_command("ATSHTTPACTION=1"):
                raise Exception("启动HTTP动作失败")
            
            # 设置请求体数据长度
            if not self.send_at_command(f"ATSHTTPDATA={content_length},10000"):
                raise Exception("设置HTTP数据长度失败")
            
            # 发送实际数据
            if not self.send_at_command(json_data):
                raise Exception("发送HTTP数据失败")
            
            # 结束请求体数据提交
            if not self.send_at_command("ATSHTTPSEND"):
                raise Exception("提交HTTP数据失败")
            
            return True
        except Exception as e:
            print(f"[4G] 上传失败: {e}")
            return False
        finally:
            # 关闭HTTP服务
            if self.http_service_active:
                self.send_at_command("ATSHTTPSERVE=0")
                self.http_service_active = False

# ============================ BLE模块 ============================
class BLEService:
    def __init__(self):
        """初始化BLE服务"""
        self.ble = bluetooth.BLE()
        self.ble.active(True)

        if not self.ble.active():
            raise RuntimeError("无法激活BLE")

        self.ble.config(gap_name=BLE_DEVICE_NAME)
        self.ble.irq(self._irq_callback)

        self.char_handle = None
        self.connected = False

        self._setup_service()
        self._start_advertising()

    def _setup_service(self):
        """注册BLE服务和特征"""
        try:
            services = self.ble.gatts_register_services([(
                SERVICE_UUID,
                ((CHAR_UUID, FLAG_READ | FLAG_WRITE),)
            )])
            
            if services and len(services) > 0:
                self.char_handle = services[0][0]
                print(f"[BLE] 特征句柄: {self.char_handle}")
            else:
                raise RuntimeError("服务注册失败")
        except Exception as e:
            print(f"[BLE] 服务设置错误: {e}")
            raise

    def _start_advertising(self):
        """开始BLE广播"""
        try:
            print("[BLE] 开始广播...")
            adv_data = bytearray()
            adv_data += b'\x02\x01\x06'  # 可连接、通用广播标志
            adv_data += b'\x03\x02' + ustruct.pack("<H", 0x1234)  # 服务UUID
            adv_data += b'\x0A\x09' + BLE_DEVICE_NAME.encode('utf-8')  # 设备名称

            self.ble.gap_advertise(100, adv_data)
        except OSError as e:
            if e.args[0] == -30:  # 资源不可用错误
                print("[BLE] 资源暂时不可用，稍后重试...")
                time.sleep(1)
                self._start_advertising()
                
    def _irq_callback(self, event, data):
        """BLE事件回调"""
        global user_data, ble_data_received

        if event == 1:  # BLE已连接
            print("[BLE] 已连接")
            self.connected = True
            rgb_flash((0, 0, 255))  # 蓝色表示BLE连接
        elif event == 2:  # BLE断开
            print("[BLE] 已断开")
            self.connected = False
            self._start_advertising()
        elif event == 3:  # 有数据写入
            try:
                conn_handle, value_handle = data
                received_data = self.ble.gatts_read(value_handle)

                if received_data:
                    try:
                        data_str = received_data.decode('ascii')
                        print(f"[BLE] 接收数据: {data_str}")
                        user_data = data_str
                        ble_data_received = True
                        rgb_flash((255, 0, 0))  # 红色表示数据接收
                    except UnicodeDecodeError:
                        print("[BLE] 警告：数据不是有效的ASCII字符串")
                else:
                    print("[BLE] ⚠️ 收到空数据")
            except Exception as e:
                print(f"[BLE] ❌ 处理数据时出错: {e}")

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
        print("[GPS] 初始化完成")

    def read_gps_data(self):
        """读取并解析GPS数据"""
        global gps_data_valid, nmea_x, nmea_y
        
        if self.uart.any():
            try:
                raw_data = self.uart.read(GPS_UART_BUFFER_SIZE)
                nmea_sentences = raw_data.decode('utf-8').split('\r\n')
                
                for sentence in nmea_sentences:
                    if sentence and 'GLL' in sentence:
                        if self._parse_gll(sentence):
                            rgb_flash((255, 255, 0))  # 黄色表示GPS数据有效
                            break
            except Exception as e:
                print(f"[GPS] 数据解析错误: {e}")

    def _parse_gll(self, sentence):
        """解析GLL语句"""
        global gps_data_valid, nmea_x, nmea_y
        
        parts = sentence.split(',')
        if len(parts) >= 7 and parts[6] == 'A':
            try:
                lat = float(parts[1])
                lon = float(parts[3])
                
                if 'S' in parts[2]:
                    lat = -lat
                if 'W' in parts[4]:
                    lon = -lon
                
                nmea_y = lat
                nmea_x = lon
                gps_data_valid = True
                
                print(f"[GPS] 有效数据 - 纬度: {nmea_y}, 经度: {nmea_x}")
                return True
            except ValueError:
                print("[GPS] 坐标转换失败")
        return False

# ============================ 数据上传模块 ============================
class DataUploader:
    @staticmethod
    def generate_payload():
        """生成要上传的数据负载"""
        global device_id, nmea_x, nmea_y, user_data
        
        try:
            current_time = time.localtime()
            time_str = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*current_time[:6])

            payload = {
                'device_id': device_id,
                'position_x': nmea_x,
                'position_y': nmea_y,
                'time_stamp': time_str,
                'user_data': user_data
            }

            print("[上传] 准备数据:")
            print(json.dumps(payload, indent=2))
            return payload
        except Exception as e:
            print(f"[上传] 生成payload出错: {e}")
            return None
    
    @staticmethod
    def upload_data():
        """上传数据到服务器"""
        global gps_data_valid, ble_data_received
        
        if not gps_data_valid or not ble_data_received:
            print("[上传] 等待GPS和BLE数据...")
            return False
        
        payload = DataUploader.generate_payload()
        if payload is None:
            return False
        
        lte = LTE4GModule()
        success = lte.send_post_request(payload)
        
        if success:
            print("[上传] 上传成功!")
            rgb_flash((0, 255, 255))  # 青色表示上传成功
            gps_data_valid = False
            ble_data_received = False
        else:
            print("[上传] 上传失败")
            
        return success

# ============================ 主程序 ============================
def main():
    """主程序入口"""
    print("\n===== ESP32-S3 数据采集系统 (4G版本) =====")
    print("[系统] 初始化BLE服务...")
    ble_service = BLEService()
    print("[系统] 初始化GPS模块...")
    gps_reader = GPSReader()
    print("[系统] 初始化4G模块...")
    lte = LTE4GModule()
    
    print("[系统] 进入主循环...")
    last_upload_time = 0
    upload_interval = 10  # 上传间隔(秒)
    
    while True:
        # 读取GPS数据
        gps_reader.read_gps_data()
        
        # 定时上传数据
        current_time = time.time()
        if current_time - last_upload_time >= upload_interval:
            if DataUploader.upload_data():
                last_upload_time = current_time
        
        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[系统] 用户终止程序")
    except Exception as e:
        print(f"\n[系统] 程序错误: {e}")
        sys.print_exception(e)
    finally:
        print("[系统] 程序结束")
        np[0] = (0, 0, 0)  # 关闭RGB灯
        np.write()