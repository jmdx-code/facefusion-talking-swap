import importlib
import logging
import os
import warnings
from types import ModuleType
from typing import Any, Dict, List, Optional

import gradio
from gradio.themes import Size

import facefusion.uis.overrides as uis_overrides
from facefusion import logger, metadata, state_manager, translator
from facefusion.exit_helper import hard_exit
from facefusion.filesystem import resolve_relative_path
from facefusion.uis.types import Component, ComponentName

UI_COMPONENTS: Dict[ComponentName, Component] = {}
UI_LAYOUT_MODULES : List[ModuleType] = []
UI_LAYOUT_METHODS =\
[
	'pre_check',
	'render',
	'listen',
	'run'
]


def load_ui_layout_module(ui_layout : str) -> Any:
	try:
		ui_layout_module = importlib.import_module('facefusion.uis.layouts.' + ui_layout)
		for method_name in UI_LAYOUT_METHODS:
			if not hasattr(ui_layout_module, method_name):
				raise NotImplementedError
	except ModuleNotFoundError as exception:
		logger.error(translator.get('ui_layout_not_loaded').format(ui_layout = ui_layout), __name__)
		logger.debug(exception.msg, __name__)
		hard_exit(1)
	except NotImplementedError:
		logger.error(translator.get('ui_layout_not_implemented').format(ui_layout = ui_layout), __name__)
		hard_exit(1)
	return ui_layout_module


def get_ui_layouts_modules(ui_layouts : List[str]) -> List[ModuleType]:
	if not UI_LAYOUT_MODULES:
		for ui_layout in ui_layouts:
			ui_layout_module = load_ui_layout_module(ui_layout)
			UI_LAYOUT_MODULES.append(ui_layout_module)
	return UI_LAYOUT_MODULES


def get_ui_component(component_name : ComponentName) -> Optional[Component]:
	if component_name in UI_COMPONENTS:
		return UI_COMPONENTS[component_name]
	return None


def get_ui_components(component_names : List[ComponentName]) -> Optional[List[Component]]:
	ui_components = []

	for component_name in component_names:
		component = get_ui_component(component_name)
		if component:
			ui_components.append(component)
	return ui_components


def register_ui_component(component_name : ComponentName, component: Component) -> None:
	UI_COMPONENTS[component_name] = component


def init() -> None:
	os.environ['GRADIO_ANALYTICS_ENABLED'] = '0'
	os.environ['GRADIO_TEMP_DIR'] = os.path.join(state_manager.get_item('temp_path'), 'gradio')

	logging.getLogger('asyncio').setLevel(logging.CRITICAL)
	warnings.filterwarnings('ignore', category = UserWarning, module = 'gradio')
	gradio.processing_utils._check_allowed = uis_overrides.mock
	gradio.processing_utils.convert_video_to_playable_mp4 = uis_overrides.convert_video_to_playable_mp4
	gradio.components.Number.raise_if_out_of_bounds = uis_overrides.mock


def launch() -> None:
	ui_layouts_total = len(state_manager.get_item('ui_layouts'))
	with gradio.Blocks(theme = get_theme(), css = get_css(), title = metadata.get('name') + ' ' + metadata.get('version'), fill_width = True) as ui:
		for ui_layout in state_manager.get_item('ui_layouts'):
			ui_layout_module = load_ui_layout_module(ui_layout)

			if ui_layouts_total > 1:
				with gradio.Tab(ui_layout):
					ui_layout_module.render()
					ui_layout_module.listen()
			else:
				ui_layout_module.render()
				ui_layout_module.listen()

	for ui_layout in state_manager.get_item('ui_layouts'):
		ui_layout_module = load_ui_layout_module(ui_layout)
		ui_layout_module.run(ui)


def get_theme() -> gradio.Theme:
	ui_font = [
		'Segoe UI',
		'system-ui',
		'-apple-system',
		'BlinkMacSystemFont',
		'sans-serif'
	]
	return gradio.themes.Base(
		primary_hue = gradio.themes.colors.indigo,
		secondary_hue = gradio.themes.colors.slate,
		neutral_hue = gradio.themes.colors.slate,
		radius_size = Size(
			xxs = '0.375rem',
			xs = '0.45rem',
			sm = '0.5rem',
			md = '0.625rem',
			lg = '0.75rem',
			xl = '0.875rem',
			xxl = '1rem',
		),
		font = ui_font
	).set(
		color_accent = '*primary_500',
		color_accent_soft = '*primary_50',
		color_accent_soft_dark = '*neutral_800',
		background_fill_primary = '#f8fafc',
		background_fill_primary_dark = '#0f172a',
		background_fill_secondary = '#f1f5f9',
		background_fill_secondary_dark = '#1e293b',
		block_background_fill = '#ffffff',
		block_background_fill_dark = '#111827',
		block_border_width = '1px',
		block_border_color = '#e2e8f0',
		block_border_color_dark = '#1f2937',
		block_label_background_fill = 'transparent',
		block_label_background_fill_dark = 'transparent',
		block_label_border_width = '0px',
		block_label_margin = '0 0 0.25rem 0',
		block_label_padding = '0.15rem 0.25rem',
		block_label_radius = '*radius_sm',
		block_label_text_color = '*neutral_700',
		block_label_text_size = '*text_sm',
		block_label_text_color_dark = '*neutral_200',
		block_label_text_weight = '600',
		block_title_background_fill = 'transparent',
		block_title_background_fill_dark = 'transparent',
		block_title_padding = '0 0 0.25rem 0',
		block_title_radius = '*radius_sm',
		block_title_text_color = '*neutral_800',
		block_title_text_color_dark = '*neutral_100',
		block_title_text_size = '*text_sm',
		block_title_text_weight = '600',
		block_padding = '0.75rem',
		border_color_accent = '*primary_300',
		border_color_accent_dark = '*primary_700',
		border_color_accent_subdued = '*neutral_200',
		border_color_accent_subdued_dark = '*neutral_700',
		border_color_primary = '#cbd5e1',
		border_color_primary_dark = '#334155',
		button_large_padding = '0.75rem 1.25rem',
		button_large_text_size = '0.95rem',
		button_large_text_weight = '600',
		button_primary_background_fill = '*primary_600',
		button_primary_background_fill_hover = '*primary_700',
		button_primary_background_fill_dark = '*primary_600',
		button_primary_background_fill_hover_dark = '*primary_500',
		button_primary_text_color = '#ffffff',
		button_secondary_background_fill = '#ffffff',
		button_secondary_background_fill_dark = '#1e293b',
		button_secondary_background_fill_hover = '#f1f5f9',
		button_secondary_background_fill_hover_dark = '#334155',
		button_secondary_border_color = '#cbd5e1',
		button_secondary_border_color_dark = '#334155',
		button_secondary_text_color = '*neutral_700',
		button_secondary_text_color_dark = '*neutral_100',
		button_small_padding = '0.45rem 0.85rem',
		button_small_text_size = '0.85rem',
		checkbox_background_color = '#ffffff',
		checkbox_background_color_dark = '#0f172a',
		checkbox_background_color_selected = '*primary_600',
		checkbox_background_color_selected_dark = '*primary_500',
		checkbox_border_color = '#94a3b8',
		checkbox_border_color_dark = '#475569',
		checkbox_label_background_fill = '#f8fafc',
		checkbox_label_background_fill_dark = '#1e293b',
		checkbox_label_background_fill_hover = '#eef2ff',
		checkbox_label_background_fill_hover_dark = '#334155',
		checkbox_label_background_fill_selected = '#eef2ff',
		checkbox_label_background_fill_selected_dark = '#312e81',
		checkbox_label_text_color_selected = '*primary_700',
		checkbox_label_text_color_selected_dark = '#e0e7ff',
		error_background_fill = '#fef2f2',
		error_background_fill_dark = '#450a0a',
		error_text_color = '#dc2626',
		error_text_color_dark = '#fca5a5',
		input_background_fill = '#f8fafc',
		input_background_fill_dark = '#0f172a',
		input_border_color = '#cbd5e1',
		input_border_color_dark = '#334155',
		input_border_width = '1px',
		shadow_drop = '0 1px 2px 0 rgba(15, 23, 42, 0.04)',
		slider_color = '*primary_600',
		slider_color_dark = '*primary_500'
	)


def get_css() -> str:
	overrides_css_path = resolve_relative_path('uis/assets/overrides.css')
	with open(overrides_css_path, encoding = 'utf-8') as css_file:
		return css_file.read()
