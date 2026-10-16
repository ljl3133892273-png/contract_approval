'''tool/logger.py'''

'''
全项目共用的全局日志对象

使用方式：
    from tool.logger import logger
    logger.info('...')

说明：
- 统一使用 colorlog 输出彩色日志，方便在终端里快速区分日志级别。
- 本模块在导入时就完成初始化，业务代码不需要再自行配置 handler。
'''

import logging

import colorlog

# 初始化全局日志对象（name 为空 → 直接返回 root logger）
logger = logging.getLogger()
# 设置日志的默认级别
logger.setLevel(logging.INFO)

# 加载彩色日志处理器
handler = colorlog.StreamHandler()
# 定义日志输出的格式
handler.setFormatter(colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(filename)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',  # INFO 显示为绿色
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'bold_red',
    }
))

# 应用日志配置信息
# 注意：模块被重复 import 时不重复挂载 handler，避免同一条日志打印多次
if not logger.handlers:
    logger.addHandler(handler)
