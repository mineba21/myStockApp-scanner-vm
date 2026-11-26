# VM 배포 · 저널 Migration · SHADOW 전환 Runbook

운영 VM에서 자동 손절(account1 미국 S01)을 OFF로 배포하고, 저널 전용 Migration을 거쳐 SHADOW로 전환하는 절차다.
VM의 Claude Code에 "Claude Code 프롬프트" 절 전체를 붙여 넣거나, 운영자가 같은 순서로 직접 실행한다.

- 관련 문서: `AGENTS.md`, `CLAUDE.md`, `docs/auto_stop_sell.md` ("저널 스키마와 Migration", "운영 절차", "외부 heartbeat").
- 대상 코드: GitHub `main` (자동 손절 저널 Migration 커밋 `2b27350` 이후). 저장소에는 자동 배포(CI/CD)가 없다. 배포·Migration은 이 절차로만 한다.
- 이 Runbook의 승인 단위: 각 ⛔ 표시 단계는 사용자 명시 승인 후에만 진행한다.

## 배포 안전 조건 (요약)

1. Migration 전 앱과 저널에 쓰는 모든 작업(앱 서비스, 저널 백업 cron, 수동 스크립트)을 멈춘다. `lsof`로 저널을 연 프로세스가 없는지 확인한다.
2. Scanner 매수 주문을 포함한 **미확정 주문이 하나라도 있으면 Migration을 중단**한다. 이번 운영 배포에서는 `--allow-unresolved`를 쓰지 않는다.
3. 운영 저널 파일이 없으면 `--allow-create`를 쓰지 않는다. `SCANNER_ORDER_JOURNAL_PATH` 설정을 확인하고 보고한다.
4. `--check` 결과를 먼저 검토하고, 사용자 명시 승인 후에만 실제 Migration을 실행한다.
5. 종료 코드 3(Migration 실패): 롤백과 원본 무결성을 확인하고 보고한다.
6. 종료 코드 4(사후 검증 실패): Migration이 이미 반영됐을 수 있다. **앱을 시작하지 않고** 원인을 확인·보고한다.
7. 실패해도 백업 파일로 자동 복원하거나 저널을 덮어쓰지 않는다.
8. 운영 DB Alembic에 예상하지 않은 신규 Migration이 없는지 확인한다. 앱 시작 시 `init_db()`가 운영 DB에 `alembic upgrade head`를 자동 실행하기 때문이다. 이번 배포의 기대 head는 `d4e8a17c5b92`(변경 없음)다.
9. Migration 후 기존 Scanner 주문 데이터 보존(건수·해시)과 스키마 준비 상태(`schema_ready=true`, `as0001`)를 확인한다.
10. 실계좌 매도·취소·LIVE 전환은 이 Runbook 범위 밖이다.

## Claude Code 프롬프트

---

이 VM은 myStockApp 운영 서버다. 먼저 AGENTS.md, CLAUDE.md, docs/auto_stop_sell.md, docs/vm_shadow_runbook.md를 읽고 지켜라.
목표: GitHub main을 OFF 상태로 배포하고, 승인 후 저널 전용 Migration을 실행하고, 키움 **조회 전용** 계약을 확인한 뒤, 내가 승인하면 SHADOW로 전환한다.
⛔ 표시 단계는 결과를 보여 주고 내 명시 승인을 받은 뒤에만 진행한다.

절대 금지
- 실계좌 매도·취소·LIVE 전환. `AUTO_STOP_US_MODE=LIVE`, `AUTO_STOP_US_REPLACE_ENABLED=true`, `KIWOOM_TRADING_ENABLED`·`ACCOUNT1_ORDER_ENABLED` 값 변경 금지.
- `.env`·자격 증명·토큰·계좌번호·원 응답 전문을 화면 출력·로그·보고에 남기지 마라. env는 아래 지정한 키만 이름=값으로 읽는다.
- 저널·OAuth 상태 DB 삭제·초기화, 백업 사본으로 복원·덮어쓰기 금지. 운영 DB·운영 Alembic 수동 변경 금지.
- `--allow-unresolved` 사용 금지. `--allow-create`는 내가 경로를 확인하고 명시 승인한 경우에만.
- 강제 push, `git reset --hard`, 작업 트리 변경 덮어쓰기 금지. 이상하면 멈추고 보고한다.
- 계좌·주문 API를 헬스 확인용으로 호출하지 마라 (조회 전용 스크립트는 5단계에서만).

1. 현재 상태 확인 (변경 없음)
   - `git status -sb`, `git log --oneline -1`. 작업 트리가 깨끗하고 현재 커밋이 origin/main의 조상인지.
   - 앱 실행 방식: `systemctl list-units --type=service | grep -i -E 'stock|uvicorn|scanner'`, `ps aux | grep -E 'uvicorn|main.py' | grep -v grep`. 서비스 이름과 실행 폴더를 기록.
   - KEY 준비 (출력 금지): `KEY=$(grep '^SITES_API_KEY=' .env | cut -d= -f2-)`
   - 진행 중 스캔 없음: `curl -s -H "Authorization: Bearer $KEY" 127.0.0.1:8000/api/scan/status`. 스캔 중이면 끝날 때까지 기다린다.
   - `grep -E '^(SCANNER_ORDER_JOURNAL_PATH|ALLOCATION_JOURNAL_PATH|AUTO_STOP_US_[A-Z_]+|KIWOOM_WEB_ENABLED)=' .env` 만 확인. `AUTO_STOP_US_MODE`가 OFF 외 값이면 멈추고 보고.
   - `SCANNER_ORDER_JOURNAL_PATH`가 비어 있거나 그 파일이 없으면 멈추고 보고한다. 경로를 임의로 정하지 않는다.
   - `crontab -l`에서 저널을 쓰거나 읽는 작업(백업 등)을 기록한다.

2. 코드 받기와 검증 (앱은 아직 기존 코드로 실행 중)
   - `git fetch origin && git log --oneline HEAD..origin/main` 으로 받을 커밋 목록 확인, `git merge-base --is-ancestor 2b27350 origin/main` 성공 확인.
   - 운영 DB Alembic 확인 (읽기만):
     - `git diff --stat HEAD origin/main -- alembic alembic.ini database` 가 비어 있어야 한다 (이번 배포는 운영 DB 스키마 변경 없음).
     - `git show origin/main:alembic/versions/ | grep '\.py$'` 목록에 새 파일이 없어야 하고, pull 후 `.venv/bin/alembic heads`가 `d4e8a17c5b92` 하나여야 한다.
     - `.venv/bin/alembic current` (운영 DB 버전 조회, 읽기 전용)가 `d4e8a17c5b92`인지 확인. 다르면 앱 시작 시 운영 DB가 자동 upgrade되므로 멈추고 보고.
   - `git pull --ff-only origin main` → `git log --oneline -1` 기록.
   - `.venv/bin/python -m pytest -q` (격리 테스트, 실주문·운영 DB 미사용). 실패하면 멈추고 보고.
   - env는 바꾸지 않는다 (AUTO_STOP_US_* 미설정 = OFF, 재주문 스위치 둘 다 false).

3. 저널에 쓰는 작업 중지와 사전 점검
   - `sudo mkdir -p /var/backups/mystockapp && sudo chown $USER /var/backups/mystockapp && chmod 700 /var/backups/mystockapp`
   - 앱 중지: `sudo systemctl stop <서비스>` → `systemctl is-active <서비스>`가 inactive인지.
   - 1단계에서 기록한 저널 관련 cron이 Migration 중 실행되지 않도록 주석 처리하거나 시간대를 확인한다 (변경했다면 기록해 두고 끝에 되돌린다).
   - 저널을 연 프로세스 없음: `lsof "$J" 2>/dev/null` 결과가 비어 있어야 한다 (`J`는 SCANNER_ORDER_JOURNAL_PATH 값). 남아 있으면 멈추고 보고.
   - 사전 점검 (변경 없음):
     `( set -a; . ./.env; set +a; .venv/bin/python -m scripts.auto_stop_journal_migrate --backup-dir /var/backups/mystockapp --check; echo "exit=$?" )`
     - 종료 코드 2 + "저널 파일이 없습니다" → 중단·보고. `--allow-create`를 쓰지 않는다.
     - 종료 코드 2 + "미확정 주문" → 중단·보고. `--allow-unresolved`를 쓰지 않는다. 앱은 OFF 그대로 다시 시작해도 된다(코드 변경만 반영, 저널 스키마 없음 → 자동 손절은 꺼진 상태).
     - 종료 코드 0이면 출력의 `tables_before`(테이블별 건수), `unresolved_before`(모두 0이어야 함), 기존 스키마에 `auto_stop_*` 객체가 있는지(`legacy_structure_diff`)를 정리해 보여 준다.
   - ⛔ 내 승인을 기다린다.

4. ⛔ 저널 Migration 실행 (승인 후, 앱 정지 상태 유지)
   - `( set -a; . ./.env; set +a; .venv/bin/python -m scripts.auto_stop_journal_migrate --backup-dir /var/backups/mystockapp; echo "exit=$?" )`
   - 종료 코드 0:
     - `"result": "OK"`, `schema_version` = `as0001`, 백업 파일 생성(파일명만 보고), 보고서 JSON(권한 600) 경로.
     - 보고서 JSON의 `before.tables`와 `after.tables`에서 기존 테이블(`scanner_buy_orders` 등)의 `rows`·`sha256`이 같은지 확인한다 (Scanner 주문 데이터 보존).
     - `.venv/bin/python -c "from trading.auto_stop.schema import schema_status; import os; s=schema_status(os.environ['SCANNER_ORDER_JOURNAL_PATH']); print({k:s[k] for k in ('schema_ready','schema_version','migration_required','schema_error')})"` (env 로드한 서브셸에서) → `schema_ready=True`.
   - 종료 코드 3 (Migration 실패, 트랜잭션 롤백):
     - 앱을 시작하지 말고 확인한다: `sqlite3` 또는 python으로 저널을 `mode=ro`로 열어 `PRAGMA integrity_check` = ok, `auto_stop_alembic_version` 표가 없음, 기존 테이블 건수가 3단계 `tables_before`와 같음.
     - 보고서 JSON의 `error`와 함께 보고하고 다음 지시를 기다린다. 백업으로 덮어쓰지 않는다.
   - 종료 코드 4 (사후 검증 실패): Migration이 이미 반영됐을 수 있다. **앱을 시작하지 않는다.** 보고서 JSON의 `problems`, `before`/`after`를 정리해 보고하고 다음 지시를 기다린다. 백업으로 복원하지 않는다.
   - 그 밖의 오류·traceback: 앱을 시작하지 않고 보고한다.

5. OFF 상태로 앱 시작과 확인 (4단계 종료 코드 0일 때만)
   - `sudo systemctl start <서비스>`, `journalctl -u <서비스> -n 50 --no-pager`에서 오류 확인. 3단계에서 바꾼 cron을 되돌린다.
   - `curl -s 127.0.0.1:8000/api/health` → `{"status":"ok"}`
   - `curl -s -H "Authorization: Bearer $KEY" 127.0.0.1:8000/api/auto-stop/health` →
     `journal_exists=true`, `schema_ready=true`, `schema_version=as0001`, `migration_required=false`, `schema_error=null`, `mode=OFF`, `replace_enabled=false`, `shadow_replace_enabled=false`, `kill_switch=null`, `unresolved_orders=0`
   - `.venv/bin/alembic current`가 여전히 `d4e8a17c5b92`인지 (운영 DB 변경 없음).
   - Scanner: `/api/scan/status` 정상, `/api/results` 응답, 다음 예약 스캔 시각. 보유종목 화면은 내가 브라우저에서 본다.
   - 저널 권한: `ls -l "$J"` 소유자=앱 실행 사용자, 권한 600 (아니면 `chmod 600`). 영속 디스크(/tmp 아님)인지.
   - 백업 cron (앱 사용자 crontab):
     `15 8,21 * * * cd <앱 폴더> && SCANNER_ORDER_JOURNAL_PATH=<경로> .venv/bin/python -m scripts.auto_stop_journal_backup --dest /var/backups/mystockapp --keep 30 >> /var/backups/mystockapp/backup.log 2>&1`
     (KST 08:15·21:15 = 미국 장 마감 후·개장 전). 한 번 수동 실행해 파일 생성과 무결성 `ok` 확인.

6. 키움 조회 전용 계약 확인 (미국 정규장 중: 평일 KST 23:30~06:00. 휴장·조기 폐장일 확인)
   - 실행 전 `grep -n "kiwoom_orders\|sell_\|cancel" scripts/auto_stop_readonly_probe.py`로 주문 모듈을 쓰지 않는지 확인.
   - env를 앱과 같게 서브셸에서:
     - `( set -a; . ./.env; set +a; .venv/bin/python -m scripts.auto_stop_readonly_probe quote --ticker <보유 미국종목> --exchange <NA|ND|NY> --samples 5 --interval 20 )`
     - `( ... balance --ticker <같은 종목> )`
     - `( ... orders --date <과거 수동 주문이 있던 YYYYMMDD> --limit 5 )`
   - 출력 원문을 보고서에 붙이지 마라. 계좌번호·주문번호처럼 보이는 값은 가린다. 다음만 정리한다:
     - quote: 필드명 목록, `dt`/`bid_tm` 갱신 간격과 최대 age(초), 비교할 시각·현재가 (키움 앱과 같은지는 내가 판정), 매수1호가 후보 필드명.
     - balance: 해당 종목 행 수(정확히 1), `sell_alowq` 값 (앱 매도가능수량과 내가 비교).
     - orders: 필드명 목록, 가격 후보 필드명, 주문별 `ord_qty/cntr_qty/ord_remnq/cncl_qty/ord_stat_nm`과 정규화 결과 (앱 주문내역과 내가 비교).
   - 내가 일치를 확인한 계약만 docs/auto_stop_sell.md "조회 전용 확인 기록" 표에 날짜·근거와 함께 적는다. `AUTO_STOP_US_VERIFIED_CONTRACTS`는 env에 넣지 않는다 (LIVE 승인 때 넣는다).
   - 문서 수정은 커밋하되 push 전에 diff를 보여 준다.

7. ⛔ SHADOW 전환 (내가 "SHADOW 전환"이라고 승인한 뒤)
   - `.env` 백업 사본을 권한 600으로 만든 뒤 다음 3줄만 추가/수정:
     `AUTO_STOP_US_MODE=SHADOW`, `AUTO_STOP_US_SHADOW_FILL_MODEL=NEXT_QUOTE`, `AUTO_STOP_US_SHADOW_REPLACE_ENABLED=false`
     (`AUTO_STOP_US_REPLACE_ENABLED`, `KIWOOM_TRADING_ENABLED`, `ACCOUNT1_ORDER_ENABLED`는 손대지 않음)
   - 재시작 후 health: `mode=SHADOW`, `schema_ready=true`, `shadow_fill_model=NEXT_QUOTE`, `shadow_replace_enabled=false`, `replace_enabled=false`, `live_blocked_reason`에 "SHADOW" 포함.
   - 정규장 중 `heartbeat_age_sec` < 60 유지, 장 시작 "감시 시작" 알림이 Slack/Telegram에 왔는지 확인.
   - 종목 등록은 내가 화면(보유종목 탭 → 미국 자동 손절 패널)에서 직접 한다. 너는 등록하지 마라.
   - 외부 heartbeat: docs/auto_stop_sell.md "외부 heartbeat" 구성대로 `~/.config/mystockapp/heartbeat.env`(600)에 `SITES_API_KEY`와 `HC_PING_URL`을 두는 cron을 안내만 한다. ping URL은 내가 만들어 넣는다. jq가 없으면 설치 여부를 묻는다.

8. 일일 보고 확인 (매 거래일 미국 마감 후)
   - `curl -s -H "Authorization: Bearer $KEY" "127.0.0.1:8000/api/auto-stop/report?day=YYYY-MM-DD&mode=SHADOW"`
   - 확인: `watch.missed=0`, `ticks.max_gap_sec ≤ 60`, `api.errors`·`api.http_429` 추세, 종목별 `max_quote_age_sec ≤ 120`, `heartbeat.ok`, `unresolved_events/orders`, `alerts.failed=0`, `shadow.current`, `shadow.mixed_models=false`.
   - NYSE 휴장일은 `trading_day=false`, 감시·주문 0. 조기 폐장일(13:00 ET)은 감시가 13:00에 끝나고 마감 요약 알림이 그 직후 도착하는지.
   - 결과를 날짜별로 운영 기록에 요약한다. 가상 체결은 실제 체결률·손익이 아님을 명시한다.

9. 장애 시 복구
   - 즉시 신규 매도·재주문 중지: 화면 "전체 긴급 중지"(STOP+사유) 또는
     `curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"enabled":true,"confirmation":"STOP","reason":"<사유>"}' 127.0.0.1:8000/api/auto-stop/kill-switch`
     (이미 접수된 증권사 주문의 취소는 보장하지 않는다. 그런 주문은 키움 앱에서 처리)
   - 기능 끄기: `.env`에서 `AUTO_STOP_US_MODE=OFF`로 바꾸고 재시작 (기존 기록 대조는 계속, 신규 감시 없음).
   - 앱이 기동하지 않으면: `journalctl`로 원인 확인 → 보고 후 지시에 따라 `git checkout 8e7a009`(자동 손절 배포 전 커밋)로 코드만 되돌리고 재시작. 저널은 그대로 두고 백업으로 덮어쓰지 않는다 (추가된 `auto_stop_*` 테이블은 이전 코드가 무시한다).
   - health가 `schema_ready=false`면 자동 손절만 멈춘 상태다 (Scanner 정상). Migration 결과를 다시 점검하고 보고한다.
   - Scanner 예약 스캔·계좌 조회 이상: `AUTO_STOP_US_MODE=OFF` 재시작으로 분리해 본 뒤 보고.

마지막에 보고: 배포 커밋, 운영 DB Alembic head/current, Migration 사전 점검·실행 결과(종료 코드, 테이블 건수 전후, 백업·보고서 파일명), 재시작 시각, health 결과, Scanner 확인 결과, 저널 권한, 조회 전용 확인 요약(민감값 제외), SHADOW 상태, 남은 일.

---

## 부록: 시세 장애 격리 수정 배포 (2026-11-26, 첫날 SHADOW FAIL 대응)

설계·정책은 `docs/auto_stop_sell.md` "시세 장애 격리와 복구"를 따른다. 위 1~2단계와 같은 방식으로 배포하되 다음을 추가로 확인한다.
⛔ 배포(앱 재시작)는 사용자 승인 후, 미국 장외 시간에만 한다.

1. 배포 전 (변경 없음)
   - 대상 커밋과 `git diff --stat <현재 배포 커밋> <대상 커밋>` 보고. `alembic/`, `database/`, `trading/auto_stop/journal_migrations/` 변경이 **없어야** 한다 (운영 DB·저널 Migration 불필요).
   - `.venv/bin/python -m pytest -q` 전체 통과.
   - 현재 저널 상태를 읽기 전용으로 기록 (mode=ro, 값 출력은 아래 키만):
     `api_errors`, `consecutive_api_errors`, `quote_global_hold`, `quote_health_version`, `global_hold_state`, 미확정 주문 수.
     첫날 이후라면 `api_errors.quote=6` 이 남아 있을 것이다. **수동으로 0으로 바꾸거나 행을 지우지 않는다.**
2. 재시작 후 첫 tick (장외)
   - health: `mode=SHADOW`, `schema_version=as0001`(변경 없음), `quote_global_hold.reason=legacy_common_counter`, `api_errors.quote` 이전 값 유지, `quote_health` 에 등록 종목 전부 `FAULT`(legacy_unattributed). 감사 기록 `quote_health_migrate` 1건.
   - 실거래 스위치·`AUTO_STOP_US_REPLACE_ENABLED`·`KIWOOM_TRADING_ENABLED`·`ACCOUNT1_ORDER_ENABLED` 값이 바뀌지 않았는지.
3. 다음 정규장 첫 5분
   - 두 종목 모두 회복 조회가 일어나는지(로그 `[자동손절] 시세 오류` JSON 또는 health `recent_quote_errors`, `quote_health[].last_ok_at`).
   - 정상이면 `RECOVERING` → `OK`, `quote_global_hold=null`, `api_errors.quote=0`, `global_hold.reasons=[]`.
   - 실패가 이어지면 `first_error`/`recent_quote_errors` 의 `category`, `http_status`, `return_code`, `message`(민감정보 제거됨)를 보고한다. 이것이 첫날 미확인 원인의 근거가 된다.
4. 장 마감 후 일일 보고: `watch.normal_rate_pct`, `watch.missed`와 분해(`held_slots`/`failed_slots`/`schedule_missed`), `recovery`, `holds.global_seconds`, `api.errors`(회복 조회 실패 포함), `api.http_requests`·`api.http_429`, 종목별 `faults`.
5. 문제가 있으면 위 9단계(장애 시 복구)를 따른다. 코드 되돌리기는 직전 배포 커밋으로만 하고 저널은 그대로 둔다 (새 runtime 키는 이전 코드가 무시한다. 다만 이전 코드는 공통 카운터 하나로 다시 판단한다).
