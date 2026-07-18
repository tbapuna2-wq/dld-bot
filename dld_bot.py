import os, re, time, shutil, tempfile, subprocess, threading, json, requests

TELEGRAM_TOKEN = "8222482916:AAGk30xlXh1klZFm-JY8pjvhI5TwMbdhV14"
CHAT_ID = "-1003971950665"

MAX_ATTEMPTS = 100
EXECUTION_TIMEOUT = 10
PIP_TIMEOUT = 30

pkg_cache = {}
cache_lock = threading.Lock()

# ---------- ФУНКЦИИ ДЛЯ РАБОТЫ С TELEGRAM ----------
def tg_request(method, data=None, files=None, retries=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    for i in range(retries):
        try:
            resp = requests.post(url, data=data, files=files, timeout=30)
            if resp.status_code == 200 and resp.json().get('ok'):
                return resp.json()
            if resp.status_code == 429:
                time.sleep(2**i)
                continue
            raise Exception(resp.text)
        except Exception as e:
            if i == retries-1: raise
            time.sleep(2**i)

def tg_upload(file_path, file_name):
    with open(file_path, 'rb') as f:
        r = tg_request('sendDocument', data={'chat_id': CHAT_ID}, files={'document': (file_name, f)})
    return r['result']['document']['file_id']

def tg_download(file_id, save_path):
    info = tg_request('getFile', data={'file_id': file_id})
    file_path = info['result']['file_path']
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    for i in range(3):
        try:
            r = requests.get(url, stream=True, timeout=30)
            if r.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(8192): f.write(chunk)
                return
            time.sleep(2**i)
        except: time.sleep(2**i)
    raise Exception("Download failed")

def send_message(chat_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={'chat_id': chat_id, 'text': text}, timeout=10)
    except: pass

# ---------- КЭШИРОВАНИЕ ПАКЕТОВ ----------
def load_cache():
    global pkg_cache
    try:
        r = tg_request('getChat', data={'chat_id': CHAT_ID})
        pinned = r['result'].get('pinned_message')
        if pinned and pinned.get('text'):
            data = json.loads(pinned['text'])
            if 'cache' in data:
                pkg_cache = data['cache']
                print(f"Загружено {len(pkg_cache)} пакетов из кэша")
    except Exception as e:
        print(f"Ошибка загрузки кэша: {e}")

def save_cache():
    try:
        payload = json.dumps({'cache': pkg_cache})
        r = tg_request('getChat', data={'chat_id': CHAT_ID})
        pinned = r['result'].get('pinned_message')
        if pinned and pinned.get('text'):
            tg_request('editMessageText', data={
                'chat_id': CHAT_ID,
                'message_id': pinned['message_id'],
                'text': payload
            })
        else:
            r = tg_request('sendMessage', data={'chat_id': CHAT_ID, 'text': payload})
            tg_request('pinChatMessage', data={'chat_id': CHAT_ID, 'message_id': r['result']['message_id']})
    except Exception as e:
        print(f"Ошибка сохранения кэша: {e}")

# ---------- УСТАНОВКА ПАКЕТОВ ----------
def get_package(pkg_name):
    with cache_lock:
        if pkg_name in pkg_cache:
            file_id = pkg_cache[pkg_name]
            lib_dir = f'/tmp/dld_libs/{pkg_name}'
            if os.path.isdir(lib_dir):
                return
            try:
                archive = f'/tmp/{pkg_name}.tar.gz'
                tg_download(file_id, archive)
                os.makedirs('/tmp/dld_libs', exist_ok=True)
                subprocess.run(['tar', '-xzf', archive, '-C', '/tmp/dld_libs'], check=True)
                os.remove(archive)
                return
            except:
                del pkg_cache[pkg_name]
                save_cache()
    tmp = tempfile.mkdtemp()
    try:
        subprocess.run(['pip', 'install', '--target', tmp, pkg_name], check=True,
                       capture_output=True, timeout=30)
        archive = os.path.join(tmp, f'{pkg_name}.tar.gz')
        subprocess.run(['tar', '-czf', archive, '-C', tmp, '.'], check=True)
        file_id = tg_upload(archive, f'{pkg_name}.tar.gz')
        os.makedirs('/tmp/dld_libs', exist_ok=True)
        subprocess.run(['tar', '-xzf', archive, '-C', '/tmp/dld_libs'], check=True)
        with cache_lock:
            pkg_cache[pkg_name] = file_id
        save_cache()
    except Exception as e:
        print(f"Ошибка установки {pkg_name}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------- ОСНОВНАЯ ЛОГИКА DLD ----------
def detect_language(code):
    if re.search(r'^\s*#!.*python|^\s*(import|from)\s+\w+|^\s*def\s+\w+', code, re.MULTILINE):
        return 'python'
    if re.search(r'^\s*#include|^\s*int\s+main', code):
        return 'c'
    if re.search(r'^\s*console\.log|^\s*function|^\s*(const|let|var)\s+|^\s*require\(', code):
        return 'node'
    return 'python'

def install_missing_packages(language, code):
    if language != 'python': return
    imports = re.findall(r'^\s*(?:import\s+(\w+)|from\s+(\w+)\s+import)', code, re.MULTILINE)
    mods = set(m[0] or m[1] for m in imports)
    std = {'os','sys','math','time','random','re','json','collections','itertools','functools','string','datetime','typing','subprocess','tempfile','shutil','threading','socket','hashlib','base64','codecs','csv','html','xml','urllib','http','json','pickle','struct','zlib','gzip','bz2','lzma','zipfile','tarfile'}
    for mod in mods:
        if mod not in std:
            get_package(mod)

def execute_code(code, language):
    tmpdir = tempfile.mkdtemp(prefix='dld_exec_')
    try:
        env = os.environ.copy()
        env['PYTHONPATH'] = '/tmp/dld_libs:' + env.get('PYTHONPATH', '')
        if language == 'python':
            path = os.path.join(tmpdir, 'script.py')
            with open(path, 'w') as f: f.write(code)
            cmd = ['python', path]
        elif language == 'c':
            src = os.path.join(tmpdir, 'prog.c')
            out = os.path.join(tmpdir, 'a.out')
            with open(src, 'w') as f: f.write(code)
            comp = subprocess.run(['gcc', src, '-o', out, '-lm'], capture_output=True, text=True)
            if comp.returncode != 0:
                return False, comp.stderr
            cmd = [out]
        elif language == 'node':
            path = os.path.join(tmpdir, 'script.js')
            with open(path, 'w') as f: f.write(code)
            cmd = ['node', path]
        else:
            return False, f"Неизвестный язык: {language}"
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=EXECUTION_TIMEOUT, env=env)
        output = res.stdout + res.stderr
        return (res.returncode == 0), output
    except subprocess.TimeoutExpired:
        return False, "Timeout (10 сек)"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def fix_code(code, error_text, language):
    if language == 'python':
        if 'NameError' in error_text:
            m = re.search(r"name '(\w+)' is not defined", error_text)
            if m:
                return f"{m.group(1)} = None\n{code}"
        if 'ModuleNotFoundError' in error_text:
            m = re.search(r"No module named '(\w+)'", error_text)
            if m:
                get_package(m.group(1))
                return code
        if 'SyntaxError' in error_text and 'unexpected EOF' in error_text:
            return code + '\n'
    elif language == 'node':
        if 'ReferenceError' in error_text:
            m = re.search(r"(\w+) is not defined", error_text)
            if m:
                return f"let {m.group(1)} = undefined;\n{code}"
    return code

def process_code(code):
    language = detect_language(code)
    os.makedirs('/tmp/dld_libs', exist_ok=True)
    install_missing_packages(language, code)
    attempts = 0
    prev = None
    last_err = ''
    while attempts < MAX_ATTEMPTS:
        ok, out = execute_code(code, language)
        if ok:
            return f"✅ Успех ({language}, попыток: {attempts+1})\n{out.strip()}"
        last_err = out
        new = fix_code(code, out, language)
        if new == code or new == prev:
            break
        prev, code = code, new
        attempts += 1
        time.sleep(0.2)
    return f"❌ Ошибка (попыток: {attempts})\n{last_err.strip()}"

# ---------- ПОЛЛИНГ БОТА ----------
def bot_poll():
    last = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {'offset': last+1, 'timeout': 30}
            resp = requests.get(url, params=params, timeout=35)
            if resp.status_code != 200:
                time.sleep(5); continue
            updates = resp.json().get('result', [])
            for u in updates:
                last = u['update_id']
                msg = u.get('message')
                if not msg: continue
                chat = msg['chat']['id']
                text = msg.get('text', '')
                if text.startswith('/start'):
                    send_message(chat, "👋 Отправьте код (Python, C, JS) для выполнения.")
                    continue
                if not text.strip():
                    send_message(chat, "❌ Пустое сообщение.")
                    continue
                result = process_code(text)
                send_message(chat, result)
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(5)

# ---------- ЗАПУСК ----------
if __name__ == '__main__':
    load_cache()
    print("Бот DLD запущен...")
    bot_poll()
  
