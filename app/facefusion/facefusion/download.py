import os
import re
import time
import urllib.request
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from tqdm import tqdm

import facefusion.choices
from facefusion import logger, metadata, process_manager, state_manager, translator
from facefusion.filesystem import get_file_name, get_file_size, is_file, remove_file
from facefusion.hash_helper import get_hash_path, validate_hash
from facefusion.types import DownloadProvider, DownloadSet

_GH_ASSET_RE = re.compile(r'/facefusion/facefusion-assets/releases/download/(?P<base>[^/]+)/(?P<file>[^/?#]+)$')
_HF_ASSET_RE = re.compile(r'/facefusion/(?P<base>[^/]+)/resolve/main/(?P<file>[^/?#]+)$')
_CHUNK_BYTES = 256 * 1024


def _normalize_proxy_url(proxy_val : str) -> Optional[str]:
	clean = (proxy_val or '').strip()
	if not clean:
		return None
	if '://' not in clean:
		clean = f'http://{clean}'
	if clean.lower().startswith(('http://', 'https://')):
		return clean
	return None


def _make_download_client(timeout : httpx.Timeout) -> httpx.Client:
	user_agent = (metadata.get('name') or 'FaceFusion') + '/' + (metadata.get('version') or '3.9.0')
	env_keys = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy')
	has_env_proxy = any(os.environ.get(k, '').strip() for k in env_keys)
	mounts : Dict[str, httpx.BaseTransport] = {
		'http://127.0.0.1': httpx.HTTPTransport(),
		'http://localhost': httpx.HTTPTransport()
	}
	if not has_env_proxy:
		try:
			sys_proxies = urllib.request.getproxies() or {}
		except Exception:
			sys_proxies = {}
		http_p = _normalize_proxy_url(sys_proxies.get('http', '') or sys_proxies.get('https', ''))
		https_p = _normalize_proxy_url(sys_proxies.get('https', '') or sys_proxies.get('http', ''))
		if http_p:
			mounts['http://'] = httpx.HTTPTransport(proxy = http_p)
		if https_p:
			mounts['https://'] = httpx.HTTPTransport(proxy = https_p)
	return httpx.Client(
		timeout = timeout,
		follow_redirects = True,
		headers = {
			'User-Agent': user_agent,
			'Accept-Encoding': 'gzip, deflate'
		},
		mounts = mounts
	)


def _extract_asset_ref(url : str) -> Optional[Tuple[str, str]]:
	path = urlparse(url).path
	for pattern in (_GH_ASSET_RE, _HF_ASSET_RE):
		match = pattern.search(path)
		if match:
			return match.group('base'), match.group('file')
	return None


def _candidate_download_urls(primary_url : str) -> List[str]:
	candidates : List[str] = []
	if primary_url:
		candidates.append(primary_url)

	asset_ref = _extract_asset_ref(primary_url)
	if asset_ref:
		base_name, file_name = asset_ref
		configured = state_manager.get_item('download_providers') or facefusion.choices.download_providers
		ordered_providers : List[DownloadProvider] = list(configured)
		for fallback_provider in facefusion.choices.download_providers:
			if fallback_provider not in ordered_providers:
				ordered_providers.append(fallback_provider)

		for provider in ordered_providers:
			provider_spec = facefusion.choices.download_provider_set.get(provider)
			if not provider_spec:
				continue
			for base_url in provider_spec.get('urls', []):
				built = base_url + provider_spec.get('path', '').format(base_name = base_name, file_name = file_name)
				if built and built not in candidates:
					candidates.append(built)

	return candidates


def _parse_total_from_headers(headers : httpx.Headers, resume_offset : int, status_code : int) -> int:
	if status_code == 206:
		content_range = headers.get('content-range', '')
		if '/' in content_range:
			tail = content_range.rsplit('/', 1)[-1].strip()
			if tail.isdigit():
				return int(tail)
		cl = headers.get('content-length', '').strip()
		if cl.isdigit():
			return resume_offset + int(cl)
	cl = headers.get('content-length', '').strip()
	if cl.isdigit():
		return int(cl)
	return 0


def _stream_download_with_failover(url : str, destination_path : str) -> bool:
	os.makedirs(os.path.dirname(destination_path), exist_ok = True)
	file_name = os.path.basename(destination_path)
	part_path = destination_path + '.part'
	candidates = _candidate_download_urls(url)
	disable_bar = state_manager.get_item('log_level') in [ 'warn', 'error' ]
	timeout = httpx.Timeout(connect = 15.0, read = 45.0, write = 30.0, pool = 15.0)
	last_error : Optional[str] = None

	for candidate_url in candidates:
		host = urlparse(candidate_url).netloc or candidate_url
		for attempt in range(2):
			resume_offset = get_file_size(part_path) if is_file(part_path) else 0
			req_headers : Dict[str, str] = {}
			if resume_offset > 0:
				req_headers['Range'] = f'bytes={resume_offset}-'

			try:
				with _make_download_client(timeout = timeout) as client:
					with client.stream('GET', candidate_url, headers = req_headers) as response:
						if response.status_code == 416 and resume_offset > 0:
							remove_file(part_path)
							resume_offset = 0
							req_headers.pop('Range', None)
							response.close()
							continue

						if response.status_code not in (200, 206):
							last_error = f'{host} HTTP {response.status_code}'
							break

						is_partial = (response.status_code == 206 and resume_offset > 0)
						initial_bytes = resume_offset if is_partial else 0
						total_bytes = _parse_total_from_headers(response.headers, initial_bytes, response.status_code)
						write_mode = 'ab' if is_partial else 'wb'
						desc = (translator.get('downloading') or 'Downloading')

						with open(part_path, write_mode) as out_stream:
							with tqdm(
								total = total_bytes if total_bytes > 0 else None,
								initial = initial_bytes,
								desc = desc,
								unit = 'B',
								unit_scale = True,
								unit_divisor = 1024,
								ascii = ' =',
								disable = disable_bar
							) as progress:
								progress.set_postfix(source = host, file_name = file_name)
								written = initial_bytes
								for chunk in response.iter_bytes(chunk_size = _CHUNK_BYTES):
									if not chunk:
										continue
									out_stream.write(chunk)
									written += len(chunk)
									progress.update(len(chunk))

						if total_bytes > 0 and written < total_bytes:
							last_error = f'{host} incomplete stream ({written}/{total_bytes} bytes)'
							time.sleep(0.8)
							continue

						os.replace(part_path, destination_path)
						return True
			except (httpx.HTTPError, OSError) as exc:
				last_error = f'{host}: {exc}'
				if attempt == 0:
					time.sleep(1.0)
					continue

	if last_error:
		logger.error(f'Failed to download {file_name} ({last_error})', __name__)
	return False


def conditional_download(download_directory_path : str, urls : List[str]) -> None:
	from facefusion.model_bootstrap import validate_model_sha256

	os.makedirs(download_directory_path, exist_ok = True)
	for url in urls:
		if not url:
			continue
		download_file_name = os.path.basename(urlparse(url).path)
		download_file_path = os.path.join(download_directory_path, download_file_name)
		if is_file(download_file_path):
			if download_file_name.endswith('.hash') and get_file_size(download_file_path) > 0:
				continue
			if validate_hash(download_file_path) and validate_model_sha256(download_file_path):
				continue
			hash_path = get_hash_path(download_file_path)
			if not is_file(hash_path) and validate_model_sha256(download_file_path):
				expected_size = get_static_download_size(url)
				if expected_size > 0 and get_file_size(download_file_path) >= expected_size:
					continue
		if _stream_download_with_failover(url, download_file_path):
			if not validate_model_sha256(download_file_path):
				logger.error(f'SHA-256 verification failed for {download_file_name}; removing untrusted file.', __name__)
				remove_file(download_file_path)


@lru_cache(maxsize = 64)
def get_static_download_size(url : str) -> int:
	timeout = httpx.Timeout(connect = 8.0, read = 12.0, write = 8.0, pool = 8.0)
	for candidate_url in _candidate_download_urls(url):
		try:
			with _make_download_client(timeout = timeout) as client:
				resp = client.head(candidate_url)
				if resp.status_code == 200:
					cl = resp.headers.get('content-length', '').strip()
					if cl.isdigit():
						return int(cl)
		except httpx.HTTPError:
			continue
	return 0


@lru_cache(maxsize = 64)
def ping_static_url(url : str) -> bool:
	timeout = httpx.Timeout(connect = 6.0, read = 8.0, write = 6.0, pool = 6.0)
	try:
		with _make_download_client(timeout = timeout) as client:
			resp = client.head(url)
			return resp.status_code < 500
	except httpx.HTTPError:
		return False


def conditional_download_hashes(hash_set : DownloadSet) -> bool:
	hash_paths = [ hash_set.get(hash_key).get('path') for hash_key in hash_set.keys() ]

	process_manager.check()
	_, invalid_hash_paths = validate_hash_paths(hash_paths)
	if invalid_hash_paths:
		for index in hash_set:
			if hash_set.get(index).get('path') in invalid_hash_paths:
				invalid_hash_url = hash_set.get(index).get('url')
				if invalid_hash_url:
					download_directory_path = os.path.dirname(hash_set.get(index).get('path'))
					conditional_download(download_directory_path, [ invalid_hash_url ])

	valid_hash_paths, invalid_hash_paths = validate_hash_paths(hash_paths)

	for valid_hash_path in valid_hash_paths:
		valid_hash_file_name = get_file_name(valid_hash_path)
		logger.debug(translator.get('validating_hash_succeeded').format(hash_file_name = valid_hash_file_name), __name__)
	for invalid_hash_path in invalid_hash_paths:
		invalid_hash_file_name = get_file_name(invalid_hash_path)
		logger.error(translator.get('validating_hash_failed').format(hash_file_name = invalid_hash_file_name), __name__)

	if not invalid_hash_paths:
		process_manager.end()
	return not invalid_hash_paths


def conditional_download_sources(source_set : DownloadSet) -> bool:
	source_paths = [ source_set.get(source_key).get('path') for source_key in source_set.keys() ]

	process_manager.check()
	_, invalid_source_paths = validate_source_paths(source_paths)
	if invalid_source_paths:
		for index in source_set:
			if source_set.get(index).get('path') in invalid_source_paths:
				invalid_source_url = source_set.get(index).get('url')
				if invalid_source_url:
					download_directory_path = os.path.dirname(source_set.get(index).get('path'))
					conditional_download(download_directory_path, [ invalid_source_url ])

	valid_source_paths, invalid_source_paths = validate_source_paths(source_paths)

	for valid_source_path in valid_source_paths:
		valid_source_file_name = get_file_name(valid_source_path)
		logger.debug(translator.get('validating_source_succeeded').format(source_file_name = valid_source_file_name), __name__)
	for invalid_source_path in invalid_source_paths:
		invalid_source_file_name = get_file_name(invalid_source_path)
		logger.error(translator.get('validating_source_failed').format(source_file_name = invalid_source_file_name), __name__)

		if remove_file(invalid_source_path):
			logger.error(translator.get('deleting_corrupt_source').format(source_file_name = invalid_source_file_name), __name__)

	if not invalid_source_paths:
		process_manager.end()
	return not invalid_source_paths


def validate_hash_paths(hash_paths : List[str]) -> Tuple[List[str], List[str]]:
	valid_hash_paths = []
	invalid_hash_paths = []

	for hash_path in hash_paths:
		if is_file(hash_path) and get_file_size(hash_path) > 0:
			valid_hash_paths.append(hash_path)
		else:
			invalid_hash_paths.append(hash_path)

	return valid_hash_paths, invalid_hash_paths


def validate_source_paths(source_paths : List[str]) -> Tuple[List[str], List[str]]:
	valid_source_paths = []
	invalid_source_paths = []

	for source_path in source_paths:
		if validate_hash(source_path):
			valid_source_paths.append(source_path)
		else:
			invalid_source_paths.append(source_path)

	return valid_source_paths, invalid_source_paths


def resolve_download_url(base_name : str, file_name : str) -> Optional[str]:
	download_providers = state_manager.get_item('download_providers') or facefusion.choices.download_providers

	for download_provider in download_providers:
		download_url = resolve_download_url_by_provider(download_provider, base_name, file_name)
		if download_url:
			return download_url

	return None


def resolve_download_url_by_provider(download_provider : DownloadProvider, base_name : str, file_name : str) -> Optional[str]:
	download_provider_value = facefusion.choices.download_provider_set.get(download_provider)
	if not download_provider_value:
		return None
	urls = download_provider_value.get('urls') or []
	if urls:
		return urls[0] + download_provider_value.get('path', '').format(base_name = base_name, file_name = file_name)
	return None
