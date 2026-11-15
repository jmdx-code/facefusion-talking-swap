import importlib
from typing import Optional

from facefusion.types import Language, LocalePoolSet, Locales

LOCALE_POOL_SET : LocalePoolSet = {}
CURRENT_LANGUAGE : Language = 'en'

# Local UI overlay for this installation. FaceFusion 3.9.0 ships with English
# labels only, so translate the visible labels while preserving internal values.
ZH_UI_TRANSLATIONS =\
{
	'APPLY': '应用',
	'CLEAR': '清空',
	'START': '开始处理',
	'STOP': '停止',
	'REFRESH': '刷新',
	'SOURCE': '源人脸图片',
	'TARGET': '目标视频',
	'OUTPUT': '输出结果',
	'OUTPUT PATH': '输出路径',
	'PREVIEW': '效果预览',
	'PREVIEW FRAME': '预览帧位置',
	'PREVIEW MODE': '预览模式',
	'PREVIEW RESOLUTION': '预览分辨率',
	'PROCESSORS': '处理功能',
	'TERMINAL': '运行日志',
	'UI WORKFLOW': '操作模式',
	'WORKFLOW STRATEGY': '处理策略',
	'EXECUTION PROVIDERS': '计算设备',
	'EXECUTION THREAD COUNT': '处理线程数',
	'VIDEO MEMORY STRATEGY': '显存策略',
	'DOWNLOAD PROVIDERS': '模型下载来源',
	'LOG LEVEL': '日志级别',
	'TEMP FRAME FORMAT': '临时帧格式',
	'TRIM FRAME': '截取视频帧范围',
	'FACE SELECTOR MODE': '人脸选择模式',
	'FACE SELECTOR ORDER': '人脸选择顺序',
	'FACE SELECTOR AGE': '年龄范围',
	'FACE SELECTOR GENDER': '性别筛选',
	'FACE SELECTOR RACE': '种族筛选',
	'REFERENCE FACE': '参考目标人脸',
	'REFERENCE FACE DISTANCE': '参考人脸相似度',
	'FACE TRACKER SCORE': '人脸跟踪阈值',
	'FACE DETECTOR MODEL': '人脸检测模型',
	'FACE DETECTOR SIZE': '人脸检测尺寸',
	'FACE DETECTOR ANGLES': '人脸检测角度',
	'FACE DETECTOR MARGIN': '人脸检测边距',
	'FACE DETECTOR SCORE': '人脸检测阈值',
	'FACE LANDMARKER MODEL': '面部关键点模型',
	'FACE LANDMARKER SCORE': '面部关键点阈值',
	'FACE MASK TYPES': '人脸遮罩类型',
	'FACE MASK AREAS': '人脸遮罩区域',
	'FACE MASK REGIONS': '人脸遮罩部位',
	'FACE MASK BLUR': '人脸遮罩羽化',
	'FACE MASK PADDING TOP': '遮罩上边距',
	'FACE MASK PADDING RIGHT': '遮罩右边距',
	'FACE MASK PADDING BOTTOM': '遮罩下边距',
	'FACE MASK PADDING LEFT': '遮罩左边距',
	'FACE OCCLUDER MODEL': '遮挡处理模型',
	'FACE PARSER MODEL': '面部区域模型',
	'VOICE EXTRACTOR MODEL': '人声提取模型',
	'OUTPUT AUDIO ENCODER': '音频编码器',
	'OUTPUT AUDIO QUALITY': '输出音频质量',
	'OUTPUT AUDIO VOLUME': '输出音量',
	'OUTPUT IMAGE QUALITY': '输出图片质量',
	'OUTPUT IMAGE SCALE': '输出图片缩放',
	'OUTPUT VIDEO ENCODER': '视频编码器',
	'OUTPUT VIDEO PRESET': '视频编码速度',
	'OUTPUT VIDEO QUALITY': '输出视频质量',
	'OUTPUT VIDEO SCALE': '输出视频缩放',
	'OUTPUT VIDEO FPS': '输出视频帧率',
	'FACE SWAPPER MODEL': '换脸模型',
	'FACE SWAPPER PIXEL BOOST': '换脸像素增强',
	'FACE SWAPPER WEIGHT': '换脸强度',
	'FACE ENHANCER MODEL': '人脸增强模型',
	'FACE ENHANCER BLEND': '人脸增强混合度',
	'FACE ENHANCER WEIGHT': '人脸增强强度',
	'FRAME ENHANCER MODEL': '画面增强模型',
	'FRAME ENHANCER BLEND': '画面增强混合度',
	'LIP SYNCER MODEL': '口型同步模型',
	'LIP SYNCER WEIGHT': '口型同步强度',
	'BACKGROUND REMOVER MODEL': '背景移除模型',
	'AGE MODIFIER MODEL': '年龄修改模型',
	'AGE MODIFIER DIRECTION': '年龄修改方向',
	'EXPRESSION RESTORER MODEL': '表情恢复模型',
	'EXPRESSION RESTORER FACTOR': '表情恢复强度',
	'EXPRESSION RESTORER AREAS': '表情恢复区域',
	'DEEP SWAPPER MODEL': '深度换脸模型',
	'DEEP SWAPPER MORPH': '深度换脸融合度',
	'FRAME COLORIZER MODEL': '画面上色模型',
	'FRAME COLORIZER SIZE': '画面上色尺寸',
	'FRAME COLORIZER BLEND': '画面上色混合度',
	'FACE DEBUGGER ITEMS': '人脸调试项目',
	'JOB STATUS': '任务状态',
	'JOB ACTION': '任务操作',
	'JOB ID': '任务编号',
	'STEP INDEX': '步骤序号'
}


def __autoload__(module_name : str) -> None:
	try:
		__locales__ = importlib.import_module(module_name + '.locales')
		load(__locales__.LOCALES, module_name)
	except ImportError:
		pass


def load(__locales__ : Locales, module_name : str) -> None:
	LOCALE_POOL_SET[module_name] = __locales__


def get(notation : str, module_name : str = 'facefusion') -> Optional[str]:
	if module_name not in LOCALE_POOL_SET:
		__autoload__(module_name)

	current = LOCALE_POOL_SET.get(module_name).get(CURRENT_LANGUAGE)

	for fragment in notation.split('.'):
		if fragment in current:
			current = current.get(fragment)

			if isinstance(current, str):
				return ZH_UI_TRANSLATIONS.get(current, current)

	return None
