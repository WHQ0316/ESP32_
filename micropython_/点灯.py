from machine import Pin
import neopixel
import time

RGB_TEST_PIN = 48  # 尝试 48, 38, 47, 21 等等
np = neopixel.NeoPixel(Pin(RGB_TEST_PIN), 1)

# 测试闪烁
while True:
    np[0] = (255, 0, 0)  # 红色
    np.write()
    time.sleep(0.5)
    np[0] = (0, 255, 0)  # 绿色
    np.write()
    time.sleep(0.5)
    np[0] = (0, 0, 255)  # 蓝色
    np.write()
    time.sleep(0.5)
    np[0] = (0, 0, 0)    # 关灯
    np.write()
    time.sleep(0.5)