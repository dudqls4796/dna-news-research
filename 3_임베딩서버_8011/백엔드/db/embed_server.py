"""n8n 적재 후 임베딩 트리거 서버. POST /embed → embed_articles.py(임베딩 없는 기사만)를 실행하고 결과 줄을 돌려준다.

n8n은 Docker 안이라 GPU·모델을 못 쓰므로, 이 PC에서 이 서버를 띄워 두고 n8n "임베딩 요청" 노드가 호출한다.
실행: 백엔드/.venv/Scripts/python.exe 백엔드/db/embed_server.py  (127.0.0.1:8011만 열림. Docker Desktop은 컨테이너의 host.docker.internal:8011을 이 주소로 전달)
요청에 기사 데이터는 오가지 않는다(DB에서 직접 읽음). 동시 요청은 하나씩 처리한다.
"""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
PORT = 8011
TOKEN_HEADER = "X-Embed-Trigger"  # 우발적 호출 방지용 고정 헤더(비밀값 아님)
lock = threading.Lock()


def notify_search_warmup() -> None:
    try:
        url = os.environ.get("SEARCH_WARMUP_URL", "http://127.0.0.1:8010/api/warmup")
        with urlopen(Request(url, method="POST"), timeout=3) as response:
            if response.status != 202:
                raise ValueError(f"HTTP {response.status}")
    except Exception as exc:
        # Do not log exception details, which may include credentials.
        print(f"[embed_server] Search warmup request failed ({type(exc).__name__})", flush=True)


def run_embed() -> tuple[int, dict]:
    with lock:  # ponytail: 전역 잠금으로 한 번에 한 실행. 수집 주기(하루 4번)에 충분.
        result = {"ok": False, "result": "출력 없음", "group": "skipped"}
        for stage, script in (("result", "embed_articles.py"), ("group", "group_events.py")):
            try:
                proc = subprocess.run(
                    [sys.executable, str(HERE / script)], cwd=HERE,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
                )
                lines = [line for line in proc.stdout.splitlines() if line.strip()]
                result[stage] = lines[-1] if lines else "출력 없음"
                print(f"[embed_server] {stage}: {result[stage]} (returncode={proc.returncode})", flush=True)
                if proc.returncode != 0:
                    break
            except (OSError, subprocess.SubprocessError) as exc:
                result[stage] = f"실행 실패: {exc}"
                print(f"[embed_server] {stage}: {result[stage]}", flush=True)
                break
            if stage == "result":
                match = re.fullmatch(r"읽음=\d+ 제목없음=\d+ 신규임베딩=(\d+)", result[stage])
                if not match:
                    result["error"] = "신규임베딩 출력 파싱 실패"
                    break
                if int(match.group(1)) == 0:
                    result["ok"] = True
                    break
            else:
                result["ok"] = True
                notify_search_warmup()
        if result["group"] == "skipped":
            print(f"[embed_server] group: skipped{'; ' + result['error'] if 'error' in result else ''}", flush=True)
        return (200 if result["ok"] else 500), result


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/embed" or self.headers.get(TOKEN_HEADER) != "n8n":
            self.send_error(404)
            return
        status, result = run_embed()
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[embed_server] {self.address_string()} {fmt % args}", flush=True)


def self_test():
    from unittest.mock import patch

    def check(outputs, expected_status, expected_group, expected_calls):
        with patch("subprocess.run", side_effect=outputs) as run, \
                patch("__main__.urlopen" if __name__ == "__main__" else f"{__name__}.urlopen") as warmup:
            warmup.return_value.__enter__.return_value.status = 202
            status, result = run_embed()
        assert warmup.call_count == int(expected_status == 200 and expected_group != "skipped")
        if warmup.called:
            assert warmup.call_args.args[0].get_method() == "POST" and warmup.call_args.kwargs["timeout"] == 3
        assert status == expected_status and result["ok"] == (status == 200)
        assert result["group"] == expected_group and run.call_count == expected_calls
        assert all(call.kwargs["cwd"] == HERE and call.kwargs["timeout"] == 3600 for call in run.call_args_list)
        assert json.loads(json.dumps(result, ensure_ascii=False)) == result
        return result

    def proc(code, output):
        return subprocess.CompletedProcess([], code, stdout=output, stderr="")

    zero = proc(0, "읽음=0 제목없음=0 신규임베딩=0\n")
    five = proc(0, "읽음=5 제목없음=0 신규임베딩=5\n")
    group_line = '묶음=1 묶인기사=2 단독기사=3 저장행=2 동아일보 대표 묶음 수=1 "확인"'
    check([zero], 200, "skipped", 1)
    check([five, proc(0, group_line)], 200, group_line, 2)
    check([proc(1, '임베딩 "실패"')], 500, "skipped", 1)
    check([five, proc(1, "묶음 실패")], 500, "묶음 실패", 2)
    assert "파싱 실패" in check([proc(0, "잘못된 출력")], 500, "skipped", 1)["error"]
    check([subprocess.TimeoutExpired("embed", 3600)], 500, "skipped", 1)
    with patch("subprocess.run", side_effect=[five, proc(0, group_line)]), \
            patch(f"{__name__}.urlopen", side_effect=OSError("offline")):
        assert run_embed()[0] == 200
    with patch.dict(os.environ, {"SEARCH_WARMUP_URL": "http://127.0.0.1:9999/custom"}), \
            patch(f"{__name__}.urlopen") as warmup:
        warmup.return_value.__enter__.return_value.status = 202
        notify_search_warmup()
        assert warmup.call_args.args[0].full_url == "http://127.0.0.1:9999/custom"
    print("자체 점검 통과", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        print(f"[embed_server] 대기 중: 127.0.0.1:{PORT}/embed", flush=True)
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
