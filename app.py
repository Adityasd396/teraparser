"""TeraBox Link Parser. Standalone, single-user Flask application."""
import gzip
import json as _json_mod
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps, lru_cache
from html import escape as _tb_escape
from http.cookies import SimpleCookie as _TBCookie
from pathlib import Path as _TBPath
from tempfile import NamedTemporaryFile as _tb_tempfile
from urllib.parse import urljoin, urlparse, urlunparse, unquote, parse_qs as _tb_query

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import requests

_ASSETS = _TBPath(__file__).with_name('assets')
TERABOX_PAGE = (_ASSETS/'index.html').read_text(encoding='utf-8')
VIDEO_EXTRACTION_SCRIPT = (_ASSETS/'extract.js').read_text(encoding='utf-8')
_TB_SESSION_LOCK = threading.Lock()
_TB_DOMAINS = ('terabox.com', '1024terabox.com', 'terabox.app')



from hmac import compare_digest
from secrets import token_urlsafe
from flask.json.provider import DefaultJSONProvider

_LOCAL_HOSTS = ('localhost','127.0.0.1','::1')
_RIPPER_RELAY_TOKEN = token_urlsafe(32)

try:
    import orjson as _fast_json
except ImportError:
    _fast_json=None


class LocalJSONProvider(DefaultJSONProvider):
    sort_keys=False
    def dumps(self,obj,**kwargs):
        if _fast_json is None:
            return super().dumps(obj,**kwargs)
        option=_fast_json.OPT_NON_STR_KEYS
        if kwargs.get('indent'):option|=_fast_json.OPT_INDENT_2
        if kwargs.get('sort_keys',self.sort_keys):option|=_fast_json.OPT_SORT_KEYS
        return _fast_json.dumps(obj,default=self.default,option=option).decode('utf-8')

    def loads(self,value,**kwargs):
        if _fast_json is None or kwargs:
            return super().loads(value,**kwargs)
        return _fast_json.loads(value)


def configure_local_runtime(app):
    app.json=LocalJSONProvider(app)
    configured_origin = os.environ.get('RIPPER_PUBLIC_ORIGIN','').strip()
    public_origin = None
    if configured_origin:
        parsed = urlparse(configured_origin)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise ValueError('RIPPER_PUBLIC_ORIGIN must be one HTTPS origin, without a path.')
        public_host = (parsed.hostname, parsed.port or 443)
        hostname = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
        public_origin = 'https://' + hostname + (f':{parsed.port}' if parsed.port not in (None,443) else '')
        username = os.environ.get('RIPPER_SERVER_USER','')
        password = os.environ.get('RIPPER_SERVER_PASSWORD','')
        if not username or ':' in username or not password:
            raise ValueError('Set RIPPER_SERVER_USER and RIPPER_SERVER_PASSWORD to enable remote access.')
    app.config['RIPPER_PUBLIC_ORIGIN'] = public_origin

    @app.before_request
    def restrict_browser_origins():
        incoming = urlparse(request.host_url)
        local = incoming.hostname in _LOCAL_HOSTS and request.remote_addr in ('127.0.0.1','::1')
        expected_origin = request.host_url.rstrip('/')
        if public_origin:
            try:
                public_request = (incoming.hostname, incoming.port or 443)==public_host
            except ValueError:
                public_request = False
            if not public_request and not local:
                return jsonify(error='Use the configured TeraBox Link Parser address.'),403
            if local and request.endpoint=='terabox_media' and compare_digest(
                    request.headers.get('X-Source-Studio-Relay','').encode(),_RIPPER_RELAY_TOKEN.encode()):
                return None
            auth = request.authorization
            if (not auth or auth.type!='basic' or not compare_digest((auth.username or '').encode(),username.encode())
                    or not compare_digest((auth.password or '').encode(),password.encode())):
                return jsonify(error='Sign in to TeraBox Link Parser.'),401,{'WWW-Authenticate':'Basic realm="TeraBox Link Parser", charset="UTF-8"'}
            if public_request:
                expected_origin = public_origin
        elif not local:
            return jsonify(error='Use the local app address or configure RIPPER_PUBLIC_ORIGIN for server access.'),403
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin=request.headers.get('Origin')
            if (origin and origin!=expected_origin) or request.headers.get('Sec-Fetch-Site')=='cross-site':
                return jsonify({'error':'Open this action from your TeraBox Link Parser page.'}),403
    @app.after_request
    def private_compact_responses(response):
        response.headers.pop('Access-Control-Allow-Origin',None)
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        if (response.mimetype=='application/json' and not response.is_streamed
                and not response.headers.get('Content-Encoding')
                and not response.headers.get('Content-Disposition')):
            response.vary.add('Accept-Encoding')
            if request.accept_encodings['gzip']>0:
                content=response.get_data()
                if len(content)>=4096:
                    compact=gzip.compress(content,compresslevel=1,mtime=0)
                    if len(compact)<len(content):
                        response.set_data(compact)
                        response.headers['Content-Encoding']='gzip'
        return response


def proxy_url():
    value = os.environ.get('RIPPER_PROXY_URL', '').strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        valid = parsed.scheme in ('http', 'https', 'socks5', 'socks5h') and parsed.hostname and parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('RIPPER_PROXY_URL must be an HTTP(S) or SOCKS5 URL with a host and port')
    return value


def configure_session(session):
    value = proxy_url()
    if value:
        session.trust_env = False
        session.proxies.update({'http': value, 'https': value})
    return session


def browser_proxy_options():
    value = proxy_url()
    if not value:
        return {}
    parsed = urlparse(value)
    if parsed.scheme.startswith('socks') and (parsed.username or parsed.password):
        raise ValueError('Browser SOCKS5 routing requires a proxy without authentication; use an HTTP proxy for authenticated browser routing')
    host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
    scheme = 'socks5' if parsed.scheme == 'socks5h' else parsed.scheme
    proxy = {'server': f'{scheme}://{host}:{parsed.port}'}
    if parsed.username:
        proxy['username'] = unquote(parsed.username)
    if parsed.password:
        proxy['password'] = unquote(parsed.password)
    return {'proxy': proxy}




MIME_TYPES = {
    'video/mp4': 'mp4', 'application/mp4': 'mp4', 'audio/mp4': 'm4a',
    'video/webm': 'webm', 'audio/webm': 'webm', 'video/x-matroska': 'mkv',
    'application/vnd.apple.mpegurl': 'm3u8', 'application/x-mpegurl': 'm3u8',
    'application/dash+xml': 'mpd', 'video/mp2t': 'ts',
    'video/ogg': 'ogv', 'audio/ogg': 'ogg', 'audio/mpeg': 'mp3',
    'video/quicktime': 'mov', 'video/x-msvideo': 'avi',
}

EXTENSIONS = set(MIME_TYPES.values()) | {'m4v', 'flv', 'aac', 'wav'}

def classify_file_type(url, headers=None, sample=b''):
    """Rank independent evidence; signatures override misleading filenames/MIME.

    Confidence is an evidence ranking, not a statistical probability.
    Container identification does not imply a video track is present.
    """
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    mime = headers.get('content-type', '').split(';')[0].strip().lower()
    evidence = []
    scores = {}
    def vote(kind, weight, source):
        scores[kind] = scores.get(kind, 0) + weight
        evidence.append({'type': kind, 'source': source, 'weight': weight})
    def extension(value):
        match = re.search(r'\.([a-z0-9]+)$', value.lower())
        return match.group(1) if match and match.group(1) in EXTENSIONS else None
    ext = extension(unquote(urlparse(url).path))
    if ext:
        vote(ext, 15, 'URL extension')
    disposition = headers.get('content-disposition', '')
    match = re.search(r"filename\*?\s*=\s*(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    if match:
        ext = extension(unquote(match.group(1).strip()))
        if ext:
            vote(ext, 25, 'download filename')
    if mime in MIME_TYPES:
        vote(MIME_TYPES[mime], 50, 'Content-Type')
    data = sample[:8192]
    stripped = data.lstrip(b'\xef\xbb\xbf \r\n\t')
    signature = None
    if re.match(br'(?is)(?:<!doctype\s+html|<html\b|<head\b|<body\b)', stripped):
        signature = 'html'
    elif stripped.startswith(b'#EXTM3U'):
        signature = 'm3u8'
    elif re.search(br'<(?:[A-Za-z_][\w.-]*:)?MPD(?:\s|>)', stripped):
        signature = 'mpd'
    elif len(data) >= 12 and data[4:8] == b'ftyp':
        signature = 'mov' if data[8:12] == b'qt  ' else 'mp4'
    elif data.startswith(b'\x1a\x45\xdf\xa3'):
        # EBML alone cannot distinguish Matroska from WebM.
        signature = 'webm' if b'webm' in data else 'mkv' if b'matroska' in data else 'ebml'
    elif data.startswith(b'OggS'):
        signature = 'ogg'
    elif data.startswith(b'FLV'):
        signature = 'flv'
    elif data.startswith(b'RIFF') and data[8:12] in (b'AVI ', b'WAVE'):
        signature = 'avi' if data[8:12] == b'AVI ' else 'wav'
    elif data.startswith(b'ID3'):
        signature = 'mp3'
    elif len(data) >= 377 and all(data[i] == 0x47 for i in (0, 188, 376)):
        signature = 'ts'
    if signature:
        vote(signature, 100, 'file signature')
        kind = signature
        confidence = 100
    elif mime in ('text/html', 'application/xhtml+xml'):
        kind, confidence = 'html', 90
        vote(kind, 90, 'Content-Type')
    else:
        ranked = sorted(scores, key=scores.get, reverse=True)
        kind = ranked[0] if ranked else 'unknown'
        confidence = min(90, scores.get(kind, 0))
    return {'type': kind, 'confidence': confidence, 'mime': mime,
            'is_media': kind in EXTENSIONS or kind == 'ebml',
            'evidence': evidence, 'bytes_examined': len(data)}

def identify_stream(url, headers=None, source='network', resource_type='', frame_url=''):
    scheme = urlparse(url).scheme
    if scheme not in ('http', 'https', 'blob'):
        return None
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    file = classify_file_type(url, headers)
    path = urlparse(url).path.lower()
    kind, confidence = None, 'candidate'
    if scheme == 'blob':
        kind = 'browser_blob'
    elif file['mime'] in ('text/html', 'application/xhtml+xml'):
        return None
    elif file['type'] == 'm3u8':
        kind = 'hls_manifest'
    elif file['type'] == 'mpd':
        kind = 'dash_manifest'
    elif re.search(r'\.(m4s|cmfv|cmfa|ts)$', path):
        kind = 'segment_candidate'
    elif file['is_media']:
        kind = 'ranged_media' if 'content-range' in headers else 'media_file_candidate'
    if kind is None:
        return None
    if file['confidence'] >= 40:
        confidence = 'response_headers'
    return {'url':url, 'kind':kind, 'file_type':file['type'],
            'mime':file['mime'], 'source':source, 'resource_type':resource_type,
            'frame_url':frame_url, 'confidence':confidence,
            'download_candidate':kind == 'media_file_candidate',
            'evidence':[source]}

def normalise_url(url, base):
    full = urljoin(base, url.strip())
    p = urlparse(full)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))

def extract_api_media(payload, base_url, max_nodes=50000):
    """Iterative, bounded JSON traversal with media-key and MIME context."""
    found = {}
    stack = [(payload, '', 0, '')]
    nodes = 0
    media_keys = {'contenturl', 'playbackurl', 'videourl', 'audiourl', 'streamurl',
                  'hls', 'dash', 'hlsurl', 'dashurl', 'manifesturl'}
    while stack and nodes < max_nodes and len(found) < 1000:
        value, key, depth, mime = stack.pop()
        nodes += 1
        if depth > 25:
            continue
        if isinstance(value, dict):
            local_mime = str(value.get('mimeType') or value.get('mime_type') or value.get('type') or mime or '')[:200]
            typed = local_mime.lower().startswith(('video', 'audio')) or local_mime.lower() in MIME_TYPES
            for child_key, child in list(value.items())[:2000]:
                name = str(child_key).lower().replace('_', '')
                # In sources arrays, src/file are media candidates even without
                # extensions. Generic API resource URLs remain unclassified.
                contextual = name in ('src', 'file', 'url') and (typed or key in ('sources', 'streams', 'hls', 'dash', 'playback', 'video', 'audio', 'files', 'renditions', 'formats'))
                stack.append((child, 'contenturl' if contextual else name, depth+1, local_mime))
        elif isinstance(value, list):
            for child in value[:5000]:
                stack.append((child, key, depth+1, mime))
        elif isinstance(value, str) and len(value) <= 8192:
            if key not in media_keys and not re.search(r'\.(mp4|webm|mkv|mov|m4v|m3u8|mpd|mp3|m4s|ts)(?:[?#]|$)', value, re.I):
                continue
            if not value.strip() or value.startswith(('#', 'data:', 'javascript:', 'blob:')):
                continue
            full = normalise_url(value, base_url)
            if urlparse(full).scheme not in ('http', 'https'):
                continue
            mime = mime if '/' in mime else 'application/vnd.apple.mpegurl' if key in ('hls','hlsurl') else 'application/dash+xml' if key in ('dash','dashurl') else ''
            headers = {'content-type': mime} if mime else {}
            record = identify_stream(full, headers, source='api_json', frame_url=base_url)
            if record is None and key in media_keys:
                record = {'url':full, 'kind':'api_media_candidate', 'file_type':'unknown',
                    'mime':mime, 'source':'api_json', 'resource_type':'json', 'frame_url':base_url,
                    'confidence':'candidate', 'download_candidate':False, 'evidence':['API media field']}
            if record:
                found[full] = record
    return list(found.values())

def isolated_browser_thread(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Never pass live Playwright pages across this boundary. Each decorated
        # function creates, uses and disposes its browser on this same worker.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='browser-engine') as pool:
            return pool.submit(function, *args, **kwargs).result()
    return run

def extract_live_frame_urls(page, script, log):
    """Skip detached frames; retry navigation races once using fresh frames."""
    results = []
    completed = set()
    for attempt in range(2):
        retry = False
        if page.is_closed():
            break
        for frame in list(page.frames):
            if id(frame) in completed or frame.is_detached():
                continue
            try:
                urls = frame.evaluate(script)
                base = frame.url
                if isinstance(urls, list):
                    results.extend((url, base) for url in urls if isinstance(url, str))
                completed.add(id(frame))
            except Exception as exc:
                message = str(exc).lower()
                if frame.is_detached() or 'frame was detached' in message or 'frame has been detached' in message:
                    completed.add(id(frame))
                    retry = True
                elif 'execution context was destroyed' in message or 'cannot find context' in message:
                    retry = True
                elif page.is_closed() or 'target page, context or browser has been closed' in message:
                    return results
                else:
                    completed.add(id(frame))
                    log.append(f"   DOM extraction unavailable: {str(exc)[:120]}")
        if not retry or attempt:
            break
        try:
            page.wait_for_timeout(100)
        except Exception:
            break
    return results

def _make_pw_browser(playwright):
    return playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'],
                                      **browser_proxy_options())


def _tb_session_path():
    configured = os.environ.get('TERABOX_DATA_DIR')
    folder = _TBPath(configured) if configured else _TBPath(__file__).with_name('data')
    return folder/'terabox-session.bin'


def _tb_cipher():
    try:
        return Fernet(os.environ.get('TERABOX_SESSION_KEY','').encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError('Set TERABOX_SESSION_KEY to a generated Fernet key.') from exc


def _tb_protect(data, decrypt=False):
    cipher = _tb_cipher()
    if not decrypt:
        return cipher.encrypt(data)
    try:
        return cipher.decrypt(data)
    except InvalidToken as exc:
        raise ValueError('Saved cookies could not be decrypted. Check TERABOX_SESSION_KEY.') from exc




def _tb_save_session(cookies, user_agent=''):
    path = _tb_session_path()
    payload = {'cookies':cookies,'user_agent':user_agent} if user_agent else cookies
    encrypted = _tb_protect(_json_mod.dumps(payload).encode('utf-8'))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with _tb_tempfile(dir=path.parent, delete=False) as output:
            temporary = _TBPath(output.name)
            output.write(encrypted)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _tb_load_session(with_user_agent=False):
    user_agent = ''
    try:
        payload = _json_mod.loads(_tb_protect(_tb_session_path().read_bytes(), decrypt=True))
        if isinstance(payload,dict):
            user_agent = payload.get('user_agent','')
            payload = payload['cookies']
        cookies = [cookie for cookie in payload if _tb_cookie_domain(cookie['domain'])
                   and (cookie.get('expires', -1) <= 0 or cookie['expires'] > time.time())]
    except FileNotFoundError:
        cookies = []
    return (cookies,user_agent) if with_user_agent else cookies


class _TBInputError(ValueError):
    pass


def _tb_cookie_domain(domain):
    domain=domain.lower().lstrip('.')
    return any(domain==root or domain.endswith('.'+root) for root in _TB_DOMAINS)


def _tb_cookie_matches_host(domain,host):
    domain = domain.lower()
    return host==domain.lstrip('.') or (domain.startswith('.') and host.endswith(domain))


def _tb_cookies(raw,host='terabox.com'):
    """Preserve exported scope; never transplant unrelated-site credentials."""
    if not isinstance(raw,str) or len(raw)>16000:
        raise _TBInputError('Cookie input must be text under 16 KB. Export only TeraBox cookies.')
    raw=raw.strip()
    if not raw:return []
    rows=[]
    if raw.startswith(('[','{')):
        try:
            payload=_json_mod.loads(raw)
            rows=payload.get('cookies',[]) if isinstance(payload,dict) else payload
            if not isinstance(rows,list):raise ValueError()
        except (ValueError,RecursionError):raise _TBInputError('Invalid JSON cookie export.')
    elif '\t' in raw or '\n' in raw:
        for line in raw.splitlines():
            line=line.strip()
            if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):continue
            parts=line.split('\t')
            if len(parts)>=7 and parts[1].upper() in ('TRUE','FALSE'):
                domain=parts[0].removeprefix('#HttpOnly_')
                rows.append({'domain':domain if parts[1].upper()=='TRUE' else domain.lstrip('.'),
                    'name':parts[5],'value':parts[6],'path':parts[2],
                    'expires':int(parts[4]) if parts[4].isdigit() else -1,
                    'secure':parts[3].upper()=='TRUE','httpOnly':parts[0].startswith('#HttpOnly_')})
            elif len(parts)>=4 and '.' in parts[2]:
                rows.append({'name':parts[0],'value':parts[1],'domain':parts[2],
                    'path':parts[3],'secure':len(parts)>7 and parts[7]=='✓','httpOnly':len(parts)>6 and parts[6]=='✓'})
            else:raise _TBInputError('Unrecognized cookie table. Use JSON, Netscape cookies.txt, or tab-separated browser cookie rows.')
    else:
        if host is None:
            raise _TBInputError('Upload a cookie export that includes the original domains.')
        if '\r' in raw:raise _TBInputError('Invalid Cookie header.')
        jar=_TBCookie()
        try:jar.load(raw)
        except Exception:raise _TBInputError('Invalid Cookie header.')
        if not jar:raise _TBInputError('Use name=value; name2=value2 or a domain-scoped cookie export.')
        rows=[{'name':name,'value':v.value,'domain':host,'path':'/','secure':True} for name,v in jar.items()]
    accepted={}
    for row in rows[:300]:
        if not isinstance(row,dict):raise _TBInputError('Each exported cookie must be an object.')
        domain=row.get('domain','')
        if not isinstance(domain,str) or not _tb_cookie_domain(domain):continue
        domain=domain.lower()
        if row.get('hostOnly') is True:domain=domain.lstrip('.')
        name,value,path=row.get('name'),row.get('value'),row.get('path','/')
        if not all(isinstance(v,str) for v in (name,value,path)) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name) or any(c in value for c in ('\r','\n','\x00')) or not path.startswith('/'):
            raise _TBInputError('Invalid TeraBox cookie fields.')
        cookie={'name':name,'value':value,'domain':domain,'path':path,
                'secure':row.get('secure',True) is True,'httpOnly':row.get('httpOnly',False) is True}
        same=row.get('sameSite','')
        if isinstance(same,str) and same.lower() in ('strict','lax','none'):
            cookie['sameSite']=same.title()
        expiry=row.get('expirationDate',row.get('expires'))
        if isinstance(expiry,(int,float)) and expiry>0:
            if expiry<=time.time():continue
            cookie['expires']=expiry
        accepted[(domain,path,name)]=cookie
    if not accepted:raise _TBInputError('No usable TeraBox cookies found. Unrelated domains and expired cookies are excluded; export cookies from the actual TeraBox page.')
    return list(accepted.values())




app = Flask(__name__)
configure_local_runtime(app)

def _tb_playback_headers(browser_request):
    names = ('user-agent','referer','origin','accept','accept-language','cookie')
    return {key.lower():value for key,value in browser_request.all_headers().items() if key.lower() in names}


def _tb_original_download(page,context):
    """Observe the site's ordinary Download control without saving the file."""
    if not _tb_cookie_domain(urlparse(page.url).hostname or ''):
        return None
    name = re.compile(r'^download(?: (?:video|file))?$',re.I)
    control = (_tb_visible_one(page.get_by_role('button',name=name))
               or _tb_visible_one(page.get_by_role('link',name=name))
               or _tb_visible_one(page.get_by_text(name)))
    if control is None or not control.is_enabled():
        return None
    requests_by_url = {}
    downloads = []

    def observe_request(browser_request):
        if browser_request.method == 'GET' and _tb_media_host(browser_request.url) and len(requests_by_url)<100:
            requests_by_url[browser_request.url] = browser_request

    def observe_download(download):
        filename = download.suggested_filename
        if _tb_media_host(download.url) and re.search(r'\.(?:mp4|mkv|webm|mov|m4v|avi|wmv|flv|mpeg|mpg)$',filename,re.I):
            downloads.append((download.url,filename))

    context.on('request',observe_request)
    page.on('download',observe_download)
    try:
        control.click(timeout=1500)
        for _ in range(8):
            if downloads:
                url,filename = downloads[0]
                browser_request = requests_by_url.get(url)
                if browser_request is not None:
                    return {'url':url,'kind':'video_download','name':filename},_tb_playback_headers(browser_request)
                return None
            page.wait_for_timeout(250)
    except PWTimeout:
        pass
    finally:
        page.remove_listener('download',observe_download)
        context.remove_listener('request',observe_request)
    return None


def _tb_visible_one(locator):
    if locator.count()>12:
        return None
    visible = [item for item in locator.all() if item.is_visible()]
    return visible[0] if len(visible)==1 else None


_TB_FULL_VIDEO_PROMPT = 'Save to TeraBox to watch the full video'
_TB_FULL_VIDEO_SIGN_IN = 'TeraBox asked you to sign in for the full video. This saved session could only access the preview.'


def _tb_login_gate(page):
    return (_tb_cookie_domain(urlparse(page.url).hostname or '')
            and page.locator('.login-dialog').first.is_visible())


def _tb_preview_save_prompt(page, wait_for_preview_end):
    prompt = page.get_by_text(_TB_FULL_VIDEO_PROMPT,exact=True).first
    if prompt.is_visible():
        return True
    if not wait_for_preview_end:
        return False
    # The share player reveals the save prompt only after the preview ends.
    for _ in range(8):
        seeked = page.locator('video').evaluate_all('''videos => {
            const video = videos.find(item => item.getClientRects().length &&
                item.duration >= 29 && item.duration <= 31 && item.seekable.length &&
                item.seekable.end(item.seekable.length - 1) >= item.duration - 1);
            if (!video) return false;
            video.currentTime = Math.min(video.duration, video.seekable.end(video.seekable.length - 1)) - 0.25;
            video.muted = true;
            video.play().catch(() => {});
            return true;
        }''')
        if seeked:
            try:
                prompt.wait_for(state='visible',timeout=5500)
                return True
            except PWTimeout:
                return False
        page.wait_for_timeout(300)
    return False


def _tb_save_preview_and_download(page,context):
    """Follow the provider's save and file controls for one identified video."""
    pattern = re.compile(r'^[^\r\n]{1,240}\.(?:mp4|mkv|webm|mov|m4v|avi|wmv|flv|mpeg|mpg)$',re.I)
    names = {name.strip() for name in page.get_by_text(pattern).all_inner_texts() if pattern.fullmatch(name.strip())}
    if not names:
        title = page.title().strip()
        if pattern.fullmatch(title):
            names.add(title)
    if len(names)!=1:
        return None,'TeraBox did not identify a single video to save.'
    filename = names.pop()
    save = _tb_visible_one(page.get_by_role('button',name='Save',exact=True))
    if save is None:
        save = _tb_visible_one(page.get_by_text('Save',exact=True))
    if save is None:
        save = _tb_visible_one(page.get_by_role('button',name=re.compile(r'^save to terabox$',re.I)))
    if save is None:
        return None,'TeraBox did not show a single Save control for the preview.'
    save.click(timeout=1500)

    success = re.compile(r'^(?:(?:file )?saved(?: to (?:my )?terabox)? successfully|save successful|save success|file saved|saved to my terabox)[.!]?$',re.I)
    account_link = re.compile(r'^(?:view (?:saved )?file|view in terabox|go to (?:my )?terabox|my terabox)$',re.I)
    confirmed = False
    saved = False
    for _ in range(16):
        if _tb_login_gate(page):
            return None,_TB_FULL_VIDEO_SIGN_IN
        if page.get_by_text(success).first.is_visible():
            saved = True
            break
        if not confirmed:
            dialog = _tb_visible_one(page.get_by_role('dialog').filter(has_text=re.compile(r'Save to:\s*My TeraBox',re.I)))
            if dialog is not None:
                confirm = _tb_visible_one(dialog.get_by_role('button',name=re.compile(r'^(?:save|save here|confirm)$',re.I)))
                if confirm is not None:
                    confirm.click(timeout=1500)
                    confirmed = True
        page.wait_for_timeout(250)
    if not saved:
        return None,'The save was requested, but TeraBox did not confirm it. Check your account before trying again.'

    link = _tb_visible_one(page.get_by_role('link',name=account_link))
    if link is None:
        link = _tb_visible_one(page.get_by_role('button',name=account_link))
    if link is None:
        link = _tb_visible_one(page.get_by_text(account_link))
    if link is None:
        return None,'TeraBox confirmed the save. Open My TeraBox to download the file.'
    previous_pages = set(context.pages)
    link.click(timeout=1500)
    for _ in range(16):
        new_pages = [tab for tab in context.pages if tab not in previous_pages and _tb_cookie_domain(urlparse(tab.url).hostname or '')]
        account = new_pages[0] if len(new_pages)==1 else page
        if _tb_cookie_domain(urlparse(account.url).hostname or ''):
            file = _tb_visible_one(account.get_by_text(filename,exact=True))
            if file is not None and (account is not page or not account.get_by_text(_TB_FULL_VIDEO_PROMPT,exact=True).first.is_visible()):
                file.click(timeout=1500)
                account.wait_for_timeout(300)
                original = _tb_original_download(account,context)
                if original and original[0]['name']==filename:
                    return original,'TeraBox saved the video and offered the original file.'
                break
        account.wait_for_timeout(250)
    return None,'TeraBox confirmed the save, but the original download was not captured. Open My TeraBox to download the saved file.'


@isolated_browser_thread
def _tb_direct(share,cookies,user_agent='',save_preview_to_account=False):
    sources={};count=0
    media_seen_at = None
    last_track_at = None
    share_errors = {}
    failed_media_urls = set()
    relay_responses = {}
    with sync_playwright() as pw:
        browser=_make_pw_browser(pw)
        try:
            # Fresh context: never inherit another site's cookies or resolver tokens.
            options = {'user_agent':user_agent} if user_agent else {}
            options['accept_downloads'] = False
            context=browser.new_context(**options)
            if cookies: context.add_cookies(cookies)
            def observed(response):
                nonlocal count, media_seen_at, last_track_at
                if len(sources)>=250 or response.status>=400: return
                try:
                    parts = urlparse(response.url)
                    trusted_share = (parts.path.startswith(('/share/','/sharing/','/api/share/')) and
                                     (_tb_cookie_domain(parts.hostname or '') or parts.hostname=='dm.1024tera.com'))
                    if 'json' in response.headers.get('content-type','').lower():
                        if count>=80:
                            return
                        size = int(response.headers.get('content-length','0'))
                        if not size and trusted_share:
                            size = response.request.sizes().get('responseBodySize',0)
                        if 0<size<=512000:
                            count+=1
                            body=response.body()
                            if len(body)<=512000:
                                payload = _json_mod.loads(body)
                                errno = payload.get('errno') if isinstance(payload,dict) else None
                                if trusted_share and re.fullmatch(r'-?[1-9][0-9]{0,8}',str(errno)):
                                    share_errors[parts.path == '/share/streaming'] = int(errno)
                                    if parts.path == '/share/streaming':
                                        failed_media_urls.add(response.url)
                                        sources.pop(response.url,None)
                                        relay_responses.pop(response.url,None)
                                    return
                                for record in extract_api_media(payload,response.url):
                                    sources.setdefault(record['url'],record)
                        return
                    record=identify_stream(response.url,response.headers,source='terabox_browser')
                    if record:
                        failed_media_urls.discard(response.url)
                        if parts.path == '/share/streaming':
                            stream_type = _tb_query(parts.query).get('type', [''])[0]
                            quality = re.fullmatch(r'M3U8_FLV_\d+_(\d{3,4})', stream_type)
                            if quality:
                                record['quality'] = quality[1] + 'p'
                            elif stream_type.startswith('M3U8_SUBTITLE'):
                                record['kind'] = 'subtitle_track'
                        is_new = record['url'] not in sources
                        sources[record['url']]=record
                        if 200<=response.status<300 and record['kind']!='dash_manifest' and _tb_media_host(response.url):
                            relay_responses.setdefault(response.url,response)
                        if is_new and record['kind'] in (
                                'hls_manifest','dash_manifest','media_file_candidate','ranged_media','subtitle_track'):
                            try:
                                frame_host = urlparse(response.request.frame.url).hostname or ''
                            except Exception:
                                frame_host = ''
                            if _tb_cookie_domain(frame_host) or _tb_cookie_domain(parts.hostname or ''):
                                last_track_at = time.monotonic()
                                if media_seen_at is None and record['kind'] != 'subtitle_track':
                                    media_seen_at = last_track_at
                except Exception: pass
            context.on('response',observed)
            page=context.new_page();page.set_default_timeout(5000)
            response = page.goto(share,wait_until='commit',timeout=12000)
            if response and response.status in (401,403,429):
                return {'error':'TeraBox denied access or rate-limited this session.','upstream_status':response.status},502

            def attach_relay(captures):
                if captures and browser.is_connected():
                    playback_cookies = [cookie for cookie in context.cookies()
                                        if _tb_media_host('https://'+cookie['domain'].lstrip('.'))]
                    _tb_attach_media_relay(sources,captures,playback_cookies)

            download_sign_in = False
            if save_preview_to_account:
                try:
                    page.wait_for_load_state('domcontentloaded',timeout=2000)
                    original = _tb_original_download(page,context)
                    if original:
                        source,headers = original
                        sources = {source['url']:source}
                        attach_relay({source['url']:(headers,None)})
                        return {'sources':[source],'message':'TeraBox offered a direct video download.'},200
                except Exception:
                    pass
                try:
                    download_sign_in = _tb_login_gate(page)
                    if download_sign_in:
                        # Download can open a login overlay over the preview's separate Save control.
                        sources.clear()
                        relay_responses.clear()
                        share_errors.clear()
                        failed_media_urls.clear()
                        media_seen_at = last_track_at = None
                        page.reload(wait_until='domcontentloaded',timeout=8000)
                except Exception:
                    pass

            def collect_dom():
                for value,base in extract_live_frame_urls(page,VIDEO_EXTRACTION_SCRIPT,[]):
                    record=identify_stream(normalise_url(value,base or share),source='terabox_dom',frame_url=base)
                    if record and record['url'] not in failed_media_urls:
                        sources.setdefault(record['url'],record)

            deadline=time.monotonic()+15
            play_clicked_on = None
            while browser.is_connected() and not page.is_closed() and time.monotonic()<deadline:
                try:
                    page.wait_for_timeout(400)
                    if _tb_cookie_domain(urlparse(page.url).hostname or ''):
                        if media_seen_at is None:
                            try:
                                playing = page.locator('video').evaluate_all('''videos => {
                                    const attempted = window.__sourceStudioTeraPlay ??= new WeakSet();
                                    const video = videos.find(item => item.getClientRects().length &&
                                        (item.currentSrc || item.src || item.querySelector('source[src]')));
                                    if (video?.paused && !attempted.has(video)) {
                                        attempted.add(video);
                                        video.muted = true;
                                        video.play().catch(() => {});
                                    }
                                    return Boolean(video && !video.paused);
                                }''')
                                if not playing and play_clicked_on != page.url:
                                    play = page.get_by_role('button', name=re.compile(r'^play(?: video)?$',re.I))
                                    if play.count() == 1 and play.is_visible():
                                        play_clicked_on = page.url
                                        try:
                                            play.click(timeout=800)
                                        except PWTimeout:
                                            play.evaluate('''button => {
                                                if (button.matches(':disabled,[aria-disabled="true"]')) return;
                                                const rect = button.getBoundingClientRect();
                                                const top = document.elementFromPoint(rect.x + rect.width/2, rect.y + rect.height/2);
                                                if (button.contains(top)) button.click();
                                            }''',timeout=800)
                            except Exception:
                                # Sign-in and share redirects can replace the document mid-scan.
                                pass
                        collect_dom()
                    now = time.monotonic()
                    tracks_settled = media_seen_at is not None and (
                        (now-media_seen_at>=1.6 and now-last_track_at>=0.8) or now-media_seen_at>=4)
                    if tracks_settled:
                        break
                except Exception:
                    if page.is_closed() or not browser.is_connected():break
                    raise
            if media_seen_at is not None:
                captures = {}
                cached_playlists = 0
                for url, captured in relay_responses.items():
                    try:
                        headers = _tb_playback_headers(captured.request)
                        body = None
                        captures[url] = (headers,body)
                        if sources[url]['kind'] in ('hls_manifest','subtitle_track') and cached_playlists<12:
                            size = int(captured.headers.get('content-length','0')) or captured.request.sizes().get('responseBodySize',0)
                            if 0<size<=512000:
                                data = captured.body()
                                if len(data)<=512000:
                                    snapshot,duration = _tb_playlist_snapshot(data)
                                    captures[url] = (headers,snapshot)
                                    if duration is not None and sources[url]['kind']=='hls_manifest':
                                        sources[url]['duration_seconds'] = duration
                                    cached_playlists += 1
                    except Exception:
                        continue
                measured = [source['duration_seconds'] for source in sources.values()
                            if source.get('duration_seconds') is not None]
                short_playlist = bool(measured) and max(measured)<=31
                preview_notice = False
                try:
                    preview_notice = _tb_preview_save_prompt(page,False)
                except Exception:
                    pass
                original = None
                save_status = ''
                if (short_playlist or preview_notice) and browser.is_connected() and not page.is_closed():
                    if not download_sign_in:
                        try:
                            original = _tb_original_download(page,context)
                        except Exception:
                            pass
                    if original is None and save_preview_to_account and not preview_notice and short_playlist and max(measured)>=29:
                        try:
                            preview_notice = _tb_preview_save_prompt(page,True)
                        except Exception:
                            pass
                    if original is None and save_preview_to_account and preview_notice:
                        try:
                            if _tb_login_gate(page):
                                save_status = _TB_FULL_VIDEO_SIGN_IN
                            else:
                                original,save_status = _tb_save_preview_and_download(page,context)
                        except Exception:
                            save_status = 'The TeraBox save flow did not complete. Check your account before trying again.'
                if download_sign_in and not original and not save_status:
                    save_status = _TB_FULL_VIDEO_SIGN_IN
                    if original:
                        source,headers = original
                        sources = {source['url']:source,**{url:record for url,record in sources.items() if url!=source['url']}}
                        captures[source['url']] = (headers,None)
                try:
                    attach_relay(captures)
                except Exception:
                    pass
                result = {'sources':list(sources.values())[:250],
                          'message':'Media detected using your saved session.'}
                if original:
                    result['message'] = save_status or 'TeraBox offered a video file through its Download control.'
                elif short_playlist or preview_notice:
                    result['message'] = save_status or ('The longest measured playlist is short. No original video download was captured.'
                                         if short_playlist else 'TeraBox shows a full-video prompt. No original video download was captured.')
                    result['full_video_share'] = share
                return result,200
            sources = {url: record for url, record in sources.items() if url.startswith(('https://','http://'))}
            if not sources and not page.is_closed():
                host = urlparse(page.url).hostname or ''
                if cookies and _tb_cookie_domain(host) and not any(_tb_cookie_matches_host(cookie['domain'],host) for cookie in cookies):
                    return {'sources':[], 'cookie_host':host, 'full_video_share':share,
                            'message':f'The share opened on {host}, but no saved cookies match that host. Open the original share and export cookies after playback works there.'},200
            if share_errors and not sources:
                errno = share_errors.get(True) or share_errors[False]
                return {'sources':[], 'upstream_errno':errno,
                        'message':f'TeraBox returned error {errno} while resolving the share.'},200
            message='Observed media candidates. Playback outside this browser session may require the same session.' if sources else 'No media observed before the window closed or the capture expired. Try again and start the video in the TeraBox window.'
            if not sources:
                message = 'TeraBox loaded, but the scan did not observe a playable media URL.'
                if download_sign_in:
                    message = _TB_FULL_VIDEO_SIGN_IN
                if browser.is_connected() and not page.is_closed():
                    try:
                        if page.get_by_text(re.compile(r'^(?:please )?(?:sign|log) in to (?:watch|play)\b',re.I)).first.is_visible():
                            message = 'TeraBox is asking for sign-in to play this share. Replace the saved cookies with a fresh export from a session that can play it.'
                        elif page.locator('input[type="password"]').first.is_visible():
                            message = 'TeraBox displayed a password field. The saved cookies alone did not unlock playback.'
                    except Exception:
                        pass
            return {'sources':list(sources.values())[:250],'message':message},200
        finally:
            if browser.is_connected():browser.close()




@app.route('/terabox/session/import', methods=['POST'])
def terabox_session_import():
    if not _TB_SESSION_LOCK.acquire(blocking=False):
        return jsonify(error='Wait for the current TeraBox scan to finish, then upload again.'),409
    try:
        user_agent = request.headers.get('X-TeraBox-User-Agent','').strip()
        if user_agent and not re.fullmatch(r'[ -~]{1,512}',user_agent):
            return jsonify(error='Enter a single browser User-Agent under 512 characters.'),400
        raw = request.stream.read(16001)
        if len(raw)>16000:
            return jsonify(error='Export only TeraBox cookies, up to 16 KB.'),413
        cookies = _tb_cookies(raw.decode('utf-8-sig'),host=None)
        if not cookies:
            return jsonify(error='The cookie export is empty.'),400
        _tb_save_session(cookies,user_agent)
        return jsonify(message=f'Saved {len(cookies)} TeraBox cookies for future scans.')
    except _TBInputError as exc:
        return jsonify(error=str(exc)),400
    except UnicodeDecodeError:
        return jsonify(error='Use a UTF-8 cookie export.'),400
    except (OSError, ValueError):
        return jsonify(error='The server could not save the cookie file.'),502
    finally:
        _TB_SESSION_LOCK.release()


@app.route('/terabox/session/user-agent', methods=['POST'])
def terabox_session_user_agent():
    data = request.get_json(silent=True)
    user_agent = data.get('user_agent') if isinstance(data,dict) else None
    if not isinstance(user_agent,str) or not re.fullmatch(r'[ -~]{0,512}',user_agent):
        return jsonify(error='Enter a single User-Agent of up to 512 characters.'),400
    if not _TB_SESSION_LOCK.acquire(blocking=False):
        return jsonify(error='Wait for the current TeraBox scan to finish.'),409
    try:
        cookies = _tb_load_session()
        if not cookies:
            return jsonify(error='Upload a usable TeraBox cookie export first.'),422
        _tb_save_session(cookies,user_agent.strip())
        return jsonify(message='User-Agent updated for your saved TeraBox cookies.')
    except (OSError, ValueError):
        return jsonify(error='The server could not update the saved TeraBox session.'),502
    finally:
        _TB_SESSION_LOCK.release()


@app.get('/')
@app.get('/terabox')
def terabox_page():
    return TERABOX_PAGE


@app.post('/terabox/resolve')
def terabox_resolve():
    if request.content_length and request.content_length>24000:
        return jsonify(error='Request too large.'),413
    data = request.get_json(silent=True)
    try:
        if not isinstance(data,dict) or not isinstance(data.get('url'),str):
            raise ValueError
        share = data['url'].strip()
        parsed = urlparse(share)
        supported = {host for root in _TB_DOMAINS for host in (root,'www.'+root)}
        if (parsed.scheme!='https' or parsed.hostname not in supported or parsed.username or parsed.password
                or parsed.port not in (None,443) or not (parsed.path.startswith('/s/') or _tb_query(parsed.query).get('surl'))):
            raise ValueError
        save_preview = data.get('save_preview_to_account',False)
        if not isinstance(save_preview,bool):
            raise ValueError
    except ValueError:
        return jsonify(error='Enter a valid HTTPS TeraBox share link.'),400
    if not _TB_SESSION_LOCK.acquire(blocking=False):
        return jsonify(error='A TeraBox scan or session update is already running.'),409
    try:
        try:
            cookies,user_agent = _tb_load_session(with_user_agent=True)
        except (OSError,ValueError) as exc:
            return jsonify(error='The saved session could not be opened. Check the server key or upload fresh cookies.'),422
        if not cookies:
            return jsonify(error='Upload a usable TeraBox cookie export in Saved session settings.'),422
        result,status = _tb_direct(share,cookies,user_agent=user_agent,save_preview_to_account=save_preview)
        return jsonify(result),status
    except PWTimeout:
        return jsonify(error='TeraBox did not respond within the navigation timeout.'),504
    except Exception:
        return jsonify(error='The TeraBox capture could not start or finish. Check Chromium installation and server connectivity.'),502
    finally:
        _TB_SESSION_LOCK.release()


"""Short-lived local relay for media observed in a TeraBox browser session."""
from secrets import token_urlsafe as _tb_token
from urllib.parse import quote as _tb_quote
from gzip import decompress as _tb_gunzip
from shutil import which as _tb_which
import subprocess as _tb_subprocess
from tempfile import TemporaryDirectory as _TBTempDirectory
from flask import Response as _TBResponse

_TB_MEDIA = {}
_TB_MEDIA_LOCK = threading.Lock()
_TB_MEDIA_TTL = 600
_TB_MEDIA_LIMIT = 1500
_TB_EXPORT_SLOT = threading.BoundedSemaphore(1)


class _TBPlaylistError(ValueError):
    pass


@app.route('/terabox/player/hls.js')
def terabox_hls_script():
    compressed = (_ASSETS/'hls.min.js.gz').read_bytes()
    headers = {'Vary':'Accept-Encoding'}
    if request.accept_encodings['gzip']>0:
        headers['Content-Encoding'] = 'gzip'
        body = compressed
    else:
        body = _tb_gunzip(compressed)
    return _TBResponse(body,mimetype='application/javascript',headers=headers)


def _tb_expire_media(session_id):
    with _TB_MEDIA_LOCK:
        _TB_MEDIA.pop(session_id,None)


def _tb_media_host(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        if (parsed.scheme == 'https' and parsed.port in (None,443) and not (parsed.username or parsed.password)
                and (_tb_cookie_domain(host) or host in ('1024tera.com','v4.freeterabox.com','teraboxcdn.com')
                     or host.endswith(('.1024tera.com','.teraboxcdn.com')))):
            return host
    except ValueError:
        pass
    return ''


def _tb_media_entry(session_id, state, url, headers, playlist=False, body=None, filename=''):
    """Called with the media lock held. Only provider URLs get local handles."""
    if not _tb_media_host(url):
        return ''
    entry = state['urls'].get(url)
    if entry is None:
        if len(state['entries']) >= _TB_MEDIA_LIMIT:
            return ''
        entry = _tb_token(12)
        state['urls'][url] = entry
        state['entries'][entry] = (url,headers,playlist,body,filename)
    return f'/terabox/media/{session_id}/{entry}'


def _tb_attach_media_relay(sources, observed_requests, cookies):
    now = time.monotonic()
    with _TB_MEDIA_LOCK:
        for session_id in list(_TB_MEDIA):
            if _TB_MEDIA[session_id]['expires'] <= now:
                _TB_MEDIA.pop(session_id)['timer'].cancel()
        state = {'expires':now+_TB_MEDIA_TTL,'entries':{},'urls':{},'cookies':cookies,'exports':{}}
        session_id = _tb_token(18)
        for source in sources.values():
            capture = observed_requests.get(source['url'])
            if not capture:
                continue
            headers,body = capture
            playlist = source['kind'] in ('hls_manifest','subtitle_track')
            filename = source.get('name','') if source['kind']=='video_download' else ''
            local = _tb_media_entry(session_id,state,source['url'],headers,playlist,body,filename)
            if local:
                source['replay_url'] = local
                if source['kind']=='hls_manifest':
                    entry = state['urls'][source['url']]
                    state['exports'][entry] = source.get('duration_seconds')
                    source['download_url'] = f'/terabox/download/{session_id}/{entry}'
        if state['entries']:
            if len(_TB_MEDIA)>=8:
                _TB_MEDIA.pop(next(iter(_TB_MEDIA)))['timer'].cancel()
            timer = threading.Timer(_TB_MEDIA_TTL,_tb_expire_media,args=(session_id,))
            timer.daemon = True
            state['timer'] = timer
            _TB_MEDIA[session_id] = state
            timer.start()


def _tb_media_cookie_header(cookies,url):
    parts = urlparse(url)
    path = parts.path or '/'
    pairs = []
    for cookie in sorted(cookies,key=lambda c:len(c.get('path','/')),reverse=True):
        domain = cookie['domain'].lower()
        scope = cookie.get('path','/')
        expires = cookie.get('expires',-1)
        if expires>0 and expires<=time.time():
            continue
        if not _tb_cookie_matches_host(domain,parts.hostname):
            continue
        if path != scope and not path.startswith(scope.rstrip('/')+'/'):
            continue
        pairs.append(f"{cookie['name']}={cookie['value']}")
    return '; '.join(pairs)


def _tb_playlist_snapshot(data):
    """Cache stable playlists and measure the duration of ended media playlists."""
    lines = data.decode('utf-8-sig').splitlines()
    if not lines or lines[0].strip() != '#EXTM3U':
        return None,None
    ended = any(line.strip() == '#EXT-X-ENDLIST' for line in lines)
    if ended:
        durations = re.findall(r'(?m)^#EXTINF:(\d+(?:\.\d+)?)(?=,|\r?$)',data.decode('utf-8-sig'))
        duration = round(sum(map(float,durations)),2) if durations else None
        return data,duration
    if any(line.startswith(('#EXT-X-STREAM-INF:','#EXT-X-I-FRAME-STREAM-INF:')) for line in lines):
        return data,None
    # An unfinished media playlist must be fetched again when the player polls it.
    return None,None


def _tb_rewrite_playlist(body,url,session_id,state,headers):
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise _TBPlaylistError('TeraBox returned a playlist that is not valid UTF-8.') from exc
    if not text.lstrip().startswith('#EXTM3U'):
        raise _TBPlaylistError('TeraBox returned a response without an HLS playlist.')
    child_headers = {key:value for key,value in headers.items() if key not in ('cookie','accept')}

    def register(uri,playlist=False):
        target = urljoin(url,uri)
        if not _tb_media_host(target):
            host = urlparse(target).hostname or ''
            if re.fullmatch(r'[a-z0-9.-]{1,253}',host):
                raise _TBPlaylistError(f'TeraBox playlist references an unsupported media host ({host}).')
            raise _TBPlaylistError('TeraBox playlist contains an unsupported media URL.')
        local = _tb_media_entry(session_id,state,target,child_headers,
                                playlist or urlparse(target).path.lower().endswith('.m3u8'))
        if not local:
            raise _TBPlaylistError('TeraBox playlist exceeds the local relay track limit.')
        return local

    lines = []
    next_is_playlist = False
    with _TB_MEDIA_LOCK:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('#'):
                if re.search(r'\bURI=(?!")',line):
                    raise _TBPlaylistError('TeraBox playlist contains an invalid URI attribute.')
                if line.startswith('#EXT-X-STREAM-INF:'):
                    next_is_playlist = True
                is_playlist = line.startswith(('#EXT-X-MEDIA:','#EXT-X-I-FRAME-STREAM-INF:'))
                line = re.sub(r'\bURI="([^"\r\n]+)"',lambda match: f'URI="{register(match[1],is_playlist)}"',line)
            elif line:
                line = register(line,next_is_playlist)
                next_is_playlist = False
            lines.append(line)
    return '\n'.join(lines)+'\n'


@lru_cache(maxsize=2)
def _tb_hls_demuxer_options(ffmpeg):
    result = _tb_subprocess.run([ffmpeg,'-hide_banner','-h','demuxer=hls'],capture_output=True,timeout=8)
    options = ['-allowed_extensions','ALL']
    if b'-extension_picky' in result.stdout:
        options.extend(['-extension_picky','0'])
    return options


@app.route('/terabox/download/<session_id>/<entry_id>')
def terabox_download(session_id,entry_id):
    with _TB_MEDIA_LOCK:
        state = _TB_MEDIA.get(session_id)
        if not state or state['expires']<=time.monotonic() or entry_id not in state['exports']:
            return 'This video link has expired. Resolve the TeraBox share again.',404
        duration = state['exports'][entry_id]
    ffmpeg = _tb_which('ffmpeg')
    if not ffmpeg:
        return 'Install FFmpeg to download this stream as MP4.',503
    if not _TB_EXPORT_SLOT.acquire(blocking=False):
        return 'An MP4 download is already being prepared. Try again when it finishes.',409
    directory = None
    media_file = None
    streaming = False
    try:
        directory = _TBTempDirectory(prefix='terabox-export-')
        output = _TBPath(directory.name)/'terabox-video.mp4'
        port = int(request.environ['SERVER_PORT'])
        manifest = f'http://127.0.0.1:{port}/terabox/media/{session_id}/{entry_id}'
        command = [ffmpeg,'-hide_banner','-loglevel','warning','-nostdin',
                   '-progress','pipe:1','-nostats','-protocol_whitelist','http,tcp,crypto',
                   *_tb_hls_demuxer_options(ffmpeg),'-f','hls',
                   '-headers',f'X-Source-Studio-Relay: {_RIPPER_RELAY_TOKEN}\r\n','-i',manifest,
                   '-map','0:v:0','-map','0:a:0?','-c','copy','-movflags','+faststart',str(output)]
        result = _tb_subprocess.run(command,capture_output=True,timeout=300,
                                    env={**os.environ,'no_proxy':'127.0.0.1','NO_PROXY':'127.0.0.1'},
                                    creationflags=getattr(_tb_subprocess,'CREATE_NO_WINDOW',0))
        missing_segments = re.search(rb'failed to open segment|error when loading|HTTP error [45]\d\d',result.stderr,re.I)
        if result.returncode or missing_segments or not output.is_file() or not output.stat().st_size:
            return 'The stream could not be packaged as MP4. Resolve the share again if its links expired.',502
        elapsed = re.findall(rb'^out_time_us=(\d+)\r?$',result.stdout,re.M)
        if duration is not None and (not elapsed or int(elapsed[-1])/1000000 < duration-2):
            return 'The stream ended before the expected duration. Resolve the share again and retry the download.',502

        media_file = output.open('rb')
        response = _TBResponse(iter(lambda: media_file.read(256*1024),b''),mimetype='video/mp4',
                               headers={'Content-Disposition':'attachment; filename="terabox-video.mp4"',
                                        'Content-Length':str(output.stat().st_size)})
        response.call_on_close(media_file.close)
        response.call_on_close(directory.cleanup)
        streaming = True
        return response
    except _tb_subprocess.TimeoutExpired:
        return 'Preparing the MP4 timed out.',504
    except (OSError, ValueError):
        return 'The local app could not prepare the MP4.',502
    finally:
        if not streaming:
            if media_file is not None:
                media_file.close()
            if directory is not None:
                directory.cleanup()
        _TB_EXPORT_SLOT.release()


@app.route('/terabox/media/<session_id>/<entry_id>')
def terabox_media(session_id,entry_id):
    with _TB_MEDIA_LOCK:
        state = _TB_MEDIA.get(session_id)
        entry = state['entries'].get(entry_id) if state and state['expires']>time.monotonic() else None
    if not entry:
        return 'This media link has expired. Resolve the original TeraBox share again.',404
    url,headers,playlist,body,filename = entry
    session = None
    upstream = None
    streamed = False
    try:
        if body is None:
            session = requests.Session()
            session.trust_env = False
            configure_session(session)
            request_headers = dict(headers)
            request_headers['accept-encoding'] = 'identity'
            if not playlist and request.headers.get('Range'):
                request_headers['range'] = request.headers['Range']
            for _ in range(4):
                if not _tb_media_host(url):
                    return 'TeraBox redirected outside the media relay.',502
                if 'cookie' not in request_headers:
                    cookie = _tb_media_cookie_header(state['cookies'],url)
                    if cookie:
                        request_headers['cookie'] = cookie
                upstream = session.get(url,headers=request_headers,stream=True,allow_redirects=False,timeout=(6,15))
                if not upstream.is_redirect:
                    break
                url = urljoin(url,upstream.headers['Location'])
                upstream.close()
                request_headers.pop('cookie',None)
            if upstream.is_redirect:
                return 'TeraBox exceeded the media redirect limit.',502
            if upstream.status_code>=400:
                return f'TeraBox returned HTTP {upstream.status_code}. Resolve the share again to refresh its media links.',502
            mime = upstream.headers.get('Content-Type','').split(';',1)[0].lower()
            if 'json' in mime or playlist or 'mpegurl' in mime:
                data = bytearray()
                for chunk in upstream.iter_content(16384):
                    data.extend(chunk)
                    if len(data)>512000:
                        return 'TeraBox playlist or error response exceeded the relay limit.',502
                body = bytes(data)
                if 'json' in mime or body.lstrip().startswith(b'{'):
                    try:
                        error = _json_mod.loads(body)
                    except ValueError:
                        return 'TeraBox returned an unreadable JSON error instead of media.',502
                    errno = error.get('errno') if isinstance(error,dict) else None
                    code = f' {errno}' if type(errno) is int else ''
                    return f'TeraBox returned a media error{code}. Resolve the original share again.',502
            else:
                if mime in ('text/html','application/xhtml+xml','image/svg+xml'):
                    return 'TeraBox returned a webpage instead of media.',502
                forwarded = {key:upstream.headers[key] for key in ('Accept-Ranges','Content-Range') if key in upstream.headers}
                if 'Content-Length' in upstream.headers and not upstream.headers.get('Content-Encoding'):
                    forwarded['Content-Length'] = upstream.headers['Content-Length']
                if filename:
                    forwarded['Content-Disposition'] = "attachment; filename*=UTF-8''" + _tb_quote(filename[:240],safe='')
                safe_mime = mime if mime.startswith(('video/','audio/')) or mime=='text/vtt' else 'application/octet-stream'
                result = _TBResponse(upstream.iter_content(65536),status=upstream.status_code,mimetype=safe_mime,headers=forwarded)
                result.call_on_close(session.close)
                result.call_on_close(upstream.close)
                streamed = True
                return result
        rewritten = _tb_rewrite_playlist(body,url,session_id,state,headers)
        return _TBResponse(rewritten,mimetype='application/vnd.apple.mpegurl')
    except _TBPlaylistError as exc:
        return str(exc),502
    except requests.exceptions.ProxyError:
        return 'The local relay could not reach the TeraBox media server through the configured proxy.',502
    except requests.exceptions.SSLError:
        return 'The local relay could not verify the TeraBox media server’s TLS connection.',502
    except requests.exceptions.Timeout:
        return 'The TeraBox media server timed out while the local relay was fetching the stream.',504
    except (requests.RequestException,OSError):
        return 'The local relay lost its connection to the TeraBox media server.',502
    except ValueError:
        return 'TeraBox returned an invalid playlist URL or media response.',502
    finally:
        if not streamed:
            if upstream is not None:
                upstream.close()
            if session is not None:
                session.close()



if __name__ == '__main__':
    from waitress import serve

    _tb_cipher()
    bind = os.environ.get('RIPPER_BIND_HOST','127.0.0.1')
    port = int(os.environ.get('PORT') or '5050')
    if bind not in _LOCAL_HOSTS and not app.config['RIPPER_PUBLIC_ORIGIN']:
        raise SystemExit('Set RIPPER_PUBLIC_ORIGIN and the server login before binding outside loopback.')
    serve(app, host=bind, port=port, threads=16, max_request_body_size=65536)
