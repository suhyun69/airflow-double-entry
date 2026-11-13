# Airflow Double Entry

Hugging Face의 `mindweave/accounting-ledger-us` 데이터를 수집·검증하고 총계정원장·월별 시산표를 생성하고 원천과 대사하며 손익계산서·재무상태표·현금흐름표와 HTML 보고서를 만들며, 모든 검증을 통과했을 때만 최종 발행하는 파이프라인입니다.

## 실행

회계 로직은 외부 패키지 없이 Python 3.9 이상에서 실행할 수 있습니다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m accounting.cli
```

첫 실행에는 네트워크가 필요합니다. 이후 동일 revision의 파일은 체크섬을 확인하고 재사용합니다. 로컬 스냅샷만 재검증하려면:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m accounting.cli \
  --snapshot data/raw/b2f2d0890353438b4473f2c3b2d2b5e15a5132a0
```

기본 실행은 HTML 생성과 최종 발행 게이트까지 수행합니다. **현재 고정 revision에는 실제 대사 차이가 있어, HTML 초안과 검증 보고서를 저장하고 최종 발행을 차단한 뒤 종료 코드 1로 끝나는 것이 예상 동작입니다.** 전표 검증까지만 실행하려면 `--until validate`, 원장까지만은 `--until ledger`, 월별 시산표까지만은 `--until trial-balance`, 대사까지는 `--until reconcile`, 재무제표 생성까지는 `--until statements`를 추가합니다. `--until statements`도 대사를 실행하고 차이가 있으면 생성 후 종료 코드 1을 반환합니다. `--until cross-validate`는 내부 교차 검증까지, `--until report`는 HTML 초안까지 생성합니다. 기본 `--until publish`는 원천부터 다시 검증하여 최종 발행을 시도합니다. `--published-root /path/to/releases`로 발행 폴더를 지정할 수 있습니다.

`--root`로 결과 루트를, `--revision`으로 전체 40자리 commit SHA를 지정할 수 있습니다. `main`처럼 변경 가능한 참조는 받지 않습니다.

## Airflow

```sh
docker compose up --build -d
docker compose logs -f airflow
```

http://localhost:8080 에 접속하여 `accounting_quality_pipeline` DAG를 수동 실행합니다. 로컬 standalone의 사용자 이름은 `admin`이며 초기 비밀번호는 컨테이너의 `/opt/airflow/simple_auth_manager_passwords.json.generated`에서 확인합니다.

```sh
docker compose exec airflow cat /opt/airflow/simple_auth_manager_passwords.json.generated
```

DAG는 `extract_snapshot → profile_source → validate_journals → build_general_ledger → build_trial_balance` 이후 두 갈래로 실행됩니다.

```text
build_trial_balance ──→ reconcile_trial_balance → build_adjusted_trial_balance → reconcile_adjusted_trial_balance
                    └─→ build_statements → validate_statements
build_statements ──→ build_cash_flow → validate_cash_flow
모든 경로 완료 ──→ render_html → publish_results (독립 재검증)
```

모든 검증 경로가 완료되면 `render_html`을 `all_done` 조건으로 실행하여 실패·미완료 상태도 초안에 담습니다. 그 뒤 `publish_results`가 원천부터 독립 재검증합니다. 유일한 leaf인 발행 태스크가 모든 품질 조건을 다시 검사하므로 HTML 생성 성공이 앞선 검증 실패를 가리지 않습니다. 기본 strict 모드에서는 원본 대사와 발행 태스크가 실패하고 보정 태스크 2개는 upstream_failed가 됩니다. adjusted 모드에서는 원본 대사 실패를 진단 결과로 보존하고 별도 보정·재대사 및 현금흐름표 검증을 진행하여 15개 태스크가 모두 성공합니다(실행 이력 최종 집계 포함). 수집은 네트워크 오류에 대비해 두 번 재시도하고, 검증 실패는 재시도하지 않습니다. XCom에는 스냅샷·보고서 경로만 전달합니다. DAG import 시에는 다운로드나 파일 읽기를 하지 않습니다.

이 Compose는 개발용 standalone과 SQLite를 사용합니다. 원장·시산표는 현재 CSV로 저장하며, 추후 DB 적재를 추가할 때 PostgreSQL을 도입할 수 있습니다. 여러 워커로 확장하면 `/opt/airflow/data`를 모든 워커에서 접근 가능한 공유 저장소로 교체해야 합니다.

중지: `docker compose down`. 데이터와 Airflow 볼륨은 유지됩니다.

## 수집 및 보고서

- `data/raw/<revision>/`: CSV 5개와 출처 URL·SHA256·크기·라이선스를 담은 `manifest.json`
- `data/reports/<revision>/profile.json`: 프로파일링 결과(실행별 보고서 폴더 기준)
- `data/reports/<revision>/validation_summary.json`: 통과 여부·전표/라인 수·오류 유형별 건수
- `data/reports/<revision>/journal_balances.json`: 전표별 차변·대변·차액
- `data/reports/<revision>/validation_errors.json`: 오류 코드와 원천 레코드 ID
- `general_ledger.csv`, `ledger_summary.json`: 전기된 라인별 원장·누적잔액과 생성 요약
- `monthly_trial_balance.csv`, `trial_balance_summary.json`: 계정별 월별 시산표와 회사/월별 차대 합계
- `reconciliation_details.json`: 전체 계정/기간의 대사 상태·차이·관련 원천 라인 ID
- `reconciliation_source_errors.json`: 원천 시산표 키 중복·잘못된 날짜/금액·참조 오류
- `reconciliation_summary.json`: 대사 통과 여부, 누락·불일치·일치 건수
- `income_statement.csv`: 월별 및 회계연도 누적 손익계산서
- `balance_sheet.csv`: 월말 재무상태표
- `statement_accounts.csv`: 계정별 매핑, 표시 부호와 월별/연누적/기말 금액
- `statement_mapping.json`, `statements_summary.json`: 실행 시 매핑 사본과 재무제표 생성 요약
- `statement_observations.json`: 음수 자산 항목 등 실제 데이터에서 관찰한 사항
- `statement_validation_checks.json`, `statement_validation_summary.json`: 교차 검증 상세와 요약
- `report.html`, `html_report_summary.json`: 검토용 HTML 초안과 생성 시 산출물 체크섬
- `publication_summary.json`: 발행 완료 또는 차단 사유

각 재무제표 CSV의 체크섬은 해당 `income_statement_summary.json`, `balance_sheet_summary.json`, `statement_accounts_summary.json`에 기록됩니다.

추가 산출물도 같은 보고서 디렉터리에 저장됩니다. 금액은 소수 둘째 자리 문자열로 기록합니다. 원장/시산표 요약에는 스냅샷 manifest와 산출물의 체크섬이 포함되어, 다음 단계가 오래된 결과나 변경된 파일을 사용하면 중단합니다.

CLI와 Airflow 보고서는 `data/reports/<revision>/<YYYY-MM 또는 full>/<run_id의 SHA256>/`에 실행별로 저장됩니다. CLI는 실행마다 고유 ID를 생성하며 과거 결과를 덮어쓰지 않습니다. 같은 태스크를 재실행하면 같은 경로에 원자적으로 덮어씁니다. 원천 파일은 manifest 생성 후 변경을 허용하지 않으며, 체크섬이 다르면 중단합니다. 불완전한 수집은 manifest를 발행하지 않고 다음 시도에서 다시 수집합니다. DAG는 한 번에 하나만 실행합니다. CLI로 같은 스냅샷을 동시에 수집하지 마세요.

프로파일링은 테이블별 행 수, 컬럼, 공백/결측 수, 고유값 수, 중복 ID, 날짜 범위와 잘못된 날짜 수, 금액 범위·합계·잘못된 값 수, 전기 여부·분개 유형 분포를 기록합니다. CSV의 빈 문자열 또는 공백을 결측으로 집계하고, `NULL` 같은 문자열을 임의로 결측으로 변환하지 않습니다.

검증은 다음을 확인합니다.

- 필수 컬럼·CSV 행 구조·체크섬, 빈 테이블, 누락/중복 ID
- 회사·전표·계정 참조와 계정/전표의 회사 일치
- 거래일 형식, 전표당 최소 2개 라인
- 금액의 유효성: 유한한 비음수, 소수 둘째 자리 이내, 최대 1e20
- 라인당 차변·대변 중 한쪽만 양수
- **각 전표의 차변 합계 = 대변 합계** (`Decimal`, 오차 허용 없음)

전기 여부와 관계없이 모든 원천 전표를 검증합니다. `posted=True` 전표만 원장에 반영하며 `False`는 제외합니다. 다른 값이나 잘못된 전표번호는 중단합니다. 검증 오류는 보고서를 남긴 후 CLI 종료 코드 1 / Airflow 태스크 실패로 처리합니다. 스키마·파일 무결성 오류는 즉시 예외로 중단합니다. 차대 일치는 계정 분류나 거래의 경제적 타당성을 보장하지 않습니다. 재무제표와 HTML은 우선 초안으로 생성하며, 독립 재검증과 원천 대사를 모두 통과한 경우에만 로컬 최종 결과를 발행합니다.

## 원장·시산표와 대사 기준

총계정원장은 회사/계정별로 거래일 → 숫자 전표번호 → 전표 ID → 라인 ID 순서로 정렬합니다. 원천 ID를 보존하고, `running_balance = 이전 잔액 + 차변 - 대변`으로 계산합니다. 같은 날짜 내 시간 정보는 없으므로 위 정렬은 재현 가능한 표시 순서입니다.

월별 시산표는 회사별 최초~최종 원천 거래월 사이의 모든 달과 모든 계정을 포함합니다. 거래 없는 달과 사용하지 않은 계정도 포함하며, 회사별로 잔액을 분리합니다.

| 컬럼 | 정의 |
|---|---|
| `opening_balance` | 해당 월 시작 전 차변-대변 누적액 |
| `period_debit`, `period_credit` | 해당 월 발생액 |
| `cumulative_debit`, `cumulative_credit` | 최초 원천 거래부터 월말까지의 누적 차변·대변 |
| `closing_balance` | 기초잔액 + 당월 차변 - 당월 대변 |
| `debit_balance`, `credit_balance` | 기말 순잔액을 차변 또는 대변 한쪽에 표시 |

각 회사/월의 발생 차변=대변 및 기말 차변잔액=대변잔액을 검증합니다. 기초는 최초 원천 거래 이전 0으로 가정하고 원천의 opening journal을 그대로 포함합니다. 연도가 바뀌어도 임의로 손익을 초기화하거나 결산 전표를 생성하지 않습니다. 따라서 완전한 실제 장부가 아니라 **제공된 원천 분개 범위의 재구성**입니다.

원천 `trial_balances.debit_balance/credit_balance`에는 양쪽 금액이 동시에 존재합니다. 예를 들어 2024-01-31 운영 현금 계정은 차변 222,553.89 / 대변 42,077.79입니다. 이 값은 누적 차변·대변과 일치하므로, 원천 컬럼을 순잔액으로 간주하지 않고 생성 시산표의 `cumulative_debit/cumulative_credit`에 각각 대사합니다. 이 비교 기준은 고정 revision의 데이터 관찰에 따른 해석이며, 다른 revision은 의미를 재확인해야 합니다.

회사/월말/계정 키의 전체 합집합을 대사합니다. 차이는 `계산값 - 원천값`, 허용 오차는 0입니다. 원천에 없는 행은 누적 차변과 대변이 **모두 0**인 경우에만 `omitted_zero`로 허용합니다. 순잔액 0이어도 거래가 있었다면 누락으로 처리합니다. 누락된 월 전체, 계산 범위 밖의 원천 기간, 키 중복, 잘못된 월말/금액/참조도 통과시키지 않습니다.

### 실제 원천 대사 결과

- 원장 17,027행, 월별 시산표 24개월 × 60계정 = 1,440행
- 원천 380행 중 일치 288행, 금액 불일치 92행
- 계산 결과 중 원천 누락 4행, 거래 없는 0행 생략 허용 1,056행
- 총 차이 96행 모두 해당 월말 라인의 차변·대변 합계와 정확히 일치

차이는 감가상각비와 감가상각누계액 3계정에 집중됩니다. 2024-01에는 해당 4행이 없고, 이후 23개월에는 4계정씩 불일치합니다. 예를 들어 2025-12 감가상각비는 계산 누적액 17,143.28, 원천 16,582.03으로 차이 561.25이며, 해당 월말 감가상각 라인 합계와 같습니다.

이는 **원천 시산표가 해당 월말 감가상각 전표를 반영하기 전에 생성되었을 가능성**을 뒷받침합니다. 생성 시점은 원천에 없어 단정할 수 없습니다. 대사 상세에는 차이를 정확히 설명하는 월말 라인 ID·설명을 기록하지만, 이 전표들을 제외하거나 차이를 보정하지 않습니다. 따라서 최종 대사는 실패 상태를 유지합니다.

## 재무제표 계산 기준

`src/accounting/statement_mapping.json`에 60계정의 코드·유형·하위유형·표시 항목을 명시했습니다. 잔액이 0인 계정도 미매핑이면 중단합니다. 회사 내 계정코드 중복이나 매핑 유형 불일치도 허용하지 않습니다. CLI의 `--mapping /path/to/mapping.json`으로 별도 version-1 매핑을 지정할 수 있습니다. Airflow는 패키지에 포함한 매핑을 사용합니다.

손익계산서는 월별(`basis=monthly`)과 회계연도 누적(`basis=fiscal_ytd`)을 각각 생성합니다. `companies.financial_year_end_month`를 사용하므로 12월 결산 외 회계연도도 처리합니다. 매출·기타수익은 대변-차변, 원가·비용은 차변-대변으로 표시하며, 매출총이익 → 영업이익 → 순이익을 계산합니다. 세금이나 결산 조정을 추정해 추가하지 않으므로 순이익은 **제공된 계정·분개에 한정된 손익**입니다.

재무상태표는 월말 누적잔액으로 계산합니다. 자산은 차변-대변, 부채·자본은 대변-차변입니다. 감가상각누계액·매출환입·소유주 인출 등 반대 방향의 실제 잔액은 음수로 표시하여 해당 항목에서 차감합니다.

장부에 손익 마감 분개가 없으므로 재무상태표 자본은 다음처럼 연결합니다.

```text
총자본 = 장부상 자본
       + 이전 회계연도 미마감 손익
       + 현재 회계연도 누적 손익
```

현재 연도 누적손익은 회계연도 시작에 새로 집계하고, 이전 손익은 사라지지 않도록 별도 표시합니다. 원천의 이익잉여금·당기손익 계정에 이미 있는 잔액은 장부상 자본에 한 번만 포함합니다. 손익계정과 이익잉여금/당기손익 계정을 동시에 사용하는 전표는 명시적인 마감 구분 없이는 손익계산서 중복·왜곡 위험이 있어 `Unsupported profit closing/earnings transfer journals`로 중단합니다. 현재는 이런 마감 분개를 지원하지 않으며, 원천에 없던 결산 전표를 생성하지 않습니다.

교차 검증은 시산표로 생성한 결과를 **원장 라인을 독립 합산한 금액**과 비교합니다.

- 회사/기간/계정 누락·중복, 계정 매핑과 체크섬
- 월별·회계연도 누적 수익/비용 및 소계·순이익
- 자산·부채·장부상 자본과 계정별 상세의 원장 연결
- 자산 = 부채 + 자본
- 손익계산서 연누적 순이익 = 재무상태표 현재 회계연도 손익
- 이전 미마감 손익과 자본 합계, 월간 자본 증감 = 순이익 + 장부상 자본 변동

실제 원천에서는 재무제표 24개월, 손익계산서 48행, 내부 교차 검증 4,968개가 모두 일치합니다. 다만 비유동자산은 자산 취득액 없이 감가상각누계액이 기록된 원천 범위 때문에 음수입니다. 예를 들어 2025-12-31에는 -17,143.28입니다. 이 관찰은 `statement_observations.json`에 기록하며 임의 보정하지 않습니다. 기초자료 누락 여부와 원천 대사 차이를 확인하기 전에는 내부 검증 통과를 완전한 재무제표의 보증으로 해석하지 않습니다.

## 현금흐름표 (직접법)

`build_cash_flow → validate_cash_flow` 태스크를 추가했습니다. `--until statements`는 현금흐름표 생성까지, `--until cross-validate`는 현금흐름표 검증까지 함께 수행합니다. HTML과 최종 발행 게이트에도 필수 단계로 포함됩니다.

계정 매핑의 `cash_flow_activity`는 모든 계정에 `cash / operating / investing / financing` 중 하나를 지정해야 합니다. 미지정 계정은 잔액이 0이어도 중단합니다. 1-1000 운영 은행과 1-1010 저축 은행을 현금으로 지정하며, 현금 계정은 유동자산이어야 합니다. 고정자산 거래는 투자활동, 자본·소유주 인출·은행 차입금은 재무활동, 나머지는 영업활동으로 명시했습니다. 이자 등 기타수익도 현재 정책에서는 영업활동입니다.

전기된 분개를 전표 단위로 묶고 현금 라인의 차변−대변을 실제 현금 증감으로 계산합니다. 상대계정의 활동 구분으로 분류하고 모든 현금흐름에 원천 전표·현금 라인·상대 라인 ID를 남깁니다.

- 현금이 없는 전표(감가상각, 외상 매출/매입 등)는 비현금 제외 이력에 남깁니다.
- 현금 계정 사이에서만 이동한 전표는 내부 이체로 제외합니다.
- 급여처럼 상대 라인이 여러 개여도 모두 같은 활동이면 현금 라인의 실제 순지급액을 사용합니다.
- 상대계정이 여러 활동으로 나뉘면 상대 라인이 모두 같은 차변/대변 방향인 명확한 경우에만 분할합니다. 현금 유입·유출이 섞인 외부 거래나 양방향 상대 라인의 혼합 활동은 임의 배분하지 않고 중단합니다.
- 매출채권·매입채무 정산은 설정된 영업활동 정책을 따릅니다. 자산 취득을 위한 외상대금 정산 등은 원 거래 추적이 추가로 필요하므로 현재 정책을 그대로 일반화하지 않아야 합니다. 환율효과를 추정하거나 통화를 변환하지 않습니다.

회사별·월별 및 회계연도 누적 기간별로 기초 현금, 활동별 유입/유출/순현금, 순증감, 기말 현금을 계산합니다. 첫 원천 분개 전 잔액은 0이며 원천의 초기 출자는 재무활동 유입에 포함됩니다. 재무상태표에 `cash_and_cash_equivalents`를 명시해 연결합니다. 현금성자산의 범위는 위 명시 계정에 한정합니다.

검증은 생성된 원장 대신 원본의 전기된 분개를 별도로 합산하고 다음을 확인합니다.

- 현금 관련 전표·제외 전표의 누락/중복, 원천 라인 ID와 활동별 금액
- 월별/회계연도 누적 기간 및 회사별 분리, 활동별 유입−유출=순현금
- 영업+투자+재무 순현금=현금 순증감, 기초 현금+순증감=기말 현금
- 기말 현금=원천 분개 누적 현금=시산표 현금=재무상태표 계정 상세 현금=재무상태표 표시 현금

추가 파일:

- `cash_flow_statement.csv`, `cash_flow_summary.json`: 직접법 현금흐름표와 체크섬
- `cash_flow_details.csv`, `cash_flow_details_summary.json`: 전표/활동별 현금 유입·유출과 원천 라인 연결
- `cash_flow_exclusions.csv`, `cash_flow_exclusions_summary.json`: 비현금·내부 이체 제외 이력
- `cash_flow_validation_checks.json`, `cash_flow_validation_summary.json`: 검증 상세와 요약

실제 데이터는 현금흐름표 48행(24개월 × 월별/연누적), 현금 전표/활동 3,209건, 비현금 제외 3,555건, 검증 10,395개 모두 통과입니다. 2025 회계연도 기초 현금 683,292.08 + 영업 순현금 1,524,705.77 + 투자 0.00 + 재무 0.00 = 기말 현금 2,207,997.85입니다. 투자 현금흐름 0은 원천의 자산 취득 자료 부재와 함께 해석해야 하며 누락 거래를 추정해 생성하지 않습니다.

최종 발행은 현금흐름 생성·검증도 임시 디렉터리에서 다시 실행하고 모든 현금흐름 산출물을 대조합니다. 검증 실패/미완료, 분류 불가, 변조, 파일 누락 시 최신 발행본을 변경하지 않습니다.

## HTML 보고서와 최종 발행

기본 명령 실행 후 stdout의 `report` 경로 또는 실행 이력 대시보드의 보고서 링크를 브라우저로 열면 됩니다. 외부 JS·폰트·CSS 의존 없이 오프라인으로 표시되며, 같은 폴더의 CSV·JSON 다운로드 링크를 포함합니다. 회사명 등 원천 문자열은 HTML 이스케이프합니다. 브라우저 인쇄에서는 전체 표를 표시하도록 구성했습니다.

보고서는 다음을 포함합니다.

- 회사·원천 revision, 분개/시산표/원천 대사/재무제표 검증 상태
- 월별·회계연도 누적 손익계산서·현금흐름표와 월말 재무상태표
- 대사 차이와 원인 후보 분개 ID, 교차 검증 실패 상세
- 음수 자산 등 실제 관찰 사항, 계산 범위와 전체 산출물 링크

`--until report`도 품질 검증 실패가 있으면 HTML을 저장한 뒤 종료 코드 1을 반환합니다. 검증 결과가 누락되거나 재무제표 생성이 실패해도 가능한 범위의 초안에 실패/미완료 상태를 표시합니다.

최종 발행 게이트는 저장된 `passed` 값만 신뢰하지 않습니다.

1. HTML 생성 당시의 스냅샷 manifest·필수 산출물·HTML 체크섬을 확인합니다.
2. 별도 임시 디렉터리에서 프로파일링 → 전표 검증 → 원장 → 시산표 → 원천 대사 → 재무제표 → 교차 검증 → 현금흐름표 생성·검증을 재실행합니다.
3. 모든 필수 CSV·JSON을 독립 재계산 결과와 바이트 단위로 대조합니다. 누락·변경·오래된 결과·단순히 상태를 통과로 바꾼 결과를 차단합니다.
4. 통과한 산출물을 복사하고 다시 체크섬을 확인한 뒤, HTML을 재생성하여 `최종 발행`으로 표시합니다.
5. 완성된 묶음을 같은 발행 파일시스템의 staging 폴더에서 원자적으로 이름 변경하고, 마지막에 `latest.json`을 원자적으로 교체합니다.

발행은 **로컬 파일 생성**이며 웹 업로드나 외부 전송을 하지 않습니다.

```text
data/published/
  latest.json                    # 최근 성공한 release_id와 상대 HTML 경로
  <release_id>/
    report.html                  # 최종 발행 표시
    release_manifest.json        # 모든 발행 파일의 SHA256과 품질 조건
    source_manifest.json         # 원천 revision·파일 체크섬
    *.csv / *.json               # 검증된 산출물 전체
```

`release_id`는 내용 체크섬으로 결정합니다. 같은 결과를 재발행하면 기존 파일을 검증하고 재사용하며 덮어쓰지 않습니다. 실패한 시도는 이전 발행본과 `latest.json`을 변경하지 않습니다. 중단된 발행에서 완성된 디렉터리가 남더라도 `latest.json`으로 노출하기 전에는 최신 발행본이 아닙니다. 발행본의 최종 상태는 `release_manifest.json`이 나타내며, 포함된 계산 단계 요약의 `draft`는 초안 생성 당시의 기록으로 보존됩니다.

음수 자산 관찰은 별도 확인 사항으로 표시하며 자동 보정하거나 추가 발행 차단 조건으로 사용하지 않습니다. 발행 통과는 정의한 품질 검증의 통과를 뜻하며, 원천 밖의 기초자료 보완이나 외부 감사 의견을 뜻하지 않습니다.

기본 strict 모드에서는 현재 데이터의 원천 차이 96건으로 발행을 차단하며 기존 최신 포인터를 유지합니다. 명시적인 adjusted 모드에서는 아래 보정 정책을 독립 검증하고 보정 후 차이가 0건일 때 최종 발행합니다. 원본 대사 실패를 통과로 바꾸지 않습니다.

## 원천 96건 분석과 별도 보정 경로

원본 `data/raw/<revision>/` CSV·manifest, 원본 대사 결과와 재구성한 원장/월별 시산표를 수정하지 않습니다. 보정 대상은 **별도 원천 시산표 사본의 누적 차변·대변**입니다. 이미 정확한 원장·재무제표에 감가상각을 한 번 더 전기하지 않습니다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m accounting.cli \
  --snapshot data/raw/b2f2d0890353438b4473f2c3b2d2b5e15a5132a0 \
  --reconciliation-mode adjusted
```

CLI 보정 실행 결과는 고유 실행 ID가 있는 보고서 폴더에 따로 저장됩니다. 모드는 실행 JSON과 발행 manifest에 기록합니다. 이전 strict 보고서는 그대로 남습니다. `--until reconcile --reconciliation-mode adjusted`는 분석·보정본 작성·보정 후 대사까지 수행합니다. 기본 strict 모드에서는 보정본이 폴더에 있더라도 이를 자동으로 신뢰하거나 재사용하지 않습니다.

Airflow도 기본은 strict입니다. 보정 모드로 UI 서비스를 시작하려면:

```sh
ACCOUNTING_RECONCILIATION_MODE=adjusted docker compose up --build -d
```

실행 컨텍스트에 모드를 고정하여 태스크 사이에 전달하고, HTML 영수증·발행 게이트·최종 manifest의 모드가 일치해야 합니다.

### 확인된 원인과 근거

96행은 감가상각비 1계정과 감가상각누계액 3계정의 24개월입니다. 2024-01에는 해당 4행 자체가 없으며, 이후 23개월에는 4계정씩 금액이 다릅니다. 212개 전표·424개 라인의 **당월 말 감가상각**이 정확히 이 차이를 설명합니다.

| 계정 | 24개월 누락분 합계 차변 | 합계 대변 |
|---|---:|---:|
| 6-2800 감가상각비 | 17,143.28 | 0.00 |
| 1-2010 사무장비 감가상각누계액 | 0.00 | 5,206.40 |
| 1-2110 비품 감가상각누계액 | 0.00 | 6,364.32 |
| 1-2210 차량 감가상각누계액 | 0.00 | 5,572.56 |

월별 배치는 2024-01~2025-09에 차변=대변 731.72, 2025-10에 654.66, 2025-11~12에 561.25입니다. 월별 전표는 2025-10까지 9건, 마지막 두 달은 7건입니다. 원천의 전표 금액·구성 변화만 확인하며, 자산 수명 종료 같은 원인은 원천 자산대장이 없어 단정하지 않습니다.

매월 감가상각 전표번호가 다른 거래보다 뒤에 있고, 원천 시산표는 정확히 당월 감가상각 이전의 누적액과 같습니다. **시산표 생성 뒤 월말 감가상각이 추가된 것으로 추정**하지만 생성 타임스탬프가 없으므로 실제 생성 순서의 확증은 아닙니다.

이전 달 감가상각은 다음 달 원천에 이미 포함되어 있습니다. 따라서 각 월 스냅샷에 그 달 누락분만 더합니다. 예를 들어 2025-12 감가상각비는 원천 16,582.03 + 당월 561.25 = 17,143.28입니다. 이전 달 보정액을 다시 누적하지 않습니다.

### 보정 허용 조건

`correction_policy.json`에 기록한 version-1 정책은 다음 조건을 모두 요구합니다.

- 전기 완료, assets 모듈, 실제 월말 날짜, `Depreciation — `로 시작하는 전표 설명
- 전표당 정확히 2라인: 6-2800(비용/영업비용)의 차변과 허용된 누계액 계정(자산/고정자산)의 동일 금액 대변
- 계정별 차이가 당월의 해당 감가상각 전체 배치와 차변·대변 각각 일치
- 근거 전표의 상대 라인까지 전부 포함되며 회사/월별 보정 차변=대변
- 원천 키·참조·날짜·금액 오류가 없고 미설명 차이가 0건

차이를 목표 잔액에 맞춰 덮어쓰지 않습니다. 보정액은 근거 라인의 금액을 합산하여 원천 금액에 더합니다. 기타 거래·부분 배치·한쪽 계정만 누락된 불완전 전표·미설명 차이는 발행을 차단합니다.

### 추가 산출물과 발행 조건

- `reconciliation_analysis.md`: 사실/추정 구분, 계정·월별 원인 및 96행 전체 분석
- `reconciliation_analysis.json`, `reconciliation_analysis_summary.json`: 정책 적합성·증거 완전성·전표번호 순서
- `correction_journal_evidence.csv`: 원천 전표/라인 ID·source_id·금액 424라인
- `correction_log.json`: 96행의 원금액·보정액·보정 후 금액·근거 ID, 신규/기존 행 구분
- `adjusted_source_trial_balances.csv`, `adjusted_source_summary.json`: 별도 보정 원천 시산표 384행(기존 380 + 신규 4)
- `adjusted_reconciliation_*.json`: 보정 후 대사 상세·요약·원천 오류

보정 후에는 384행 일치, 거래 없는 1,056행 생략 허용, 차이 0건입니다. 내부 재무제표 검증 4,968개도 계속 통과합니다. 재무제표 금액은 이미 올바른 전기 분개로 계산했으므로 보정 전후에 바뀌지 않습니다.

adjusted 발행 게이트는 임시 디렉터리에서 원본 대사의 96건 실패를 재현하고, 정책에 따른 분석·보정·재대사를 다시 계산하여 보정본과 감사 추적 파일을 전부 대조합니다. 원본 실패를 무시하는 일반 예외 경로는 없습니다. 보정 근거 또는 재대사에서 실패하면 발행을 차단합니다. HTML과 `release_manifest.json`에 `reconciliation_mode=adjusted` 및 보정 기준임을 표시하고 원본 실패 결과도 함께 묶어 발행합니다.

## 월별 자동 실행·백필·실행 이력

DAG는 한국시간 **매월 1일 00:00**에 직전 달을 처리합니다. `CronDataIntervalTimetable`을 명시하여 Airflow 3의 빈 cron 구간 기본 동작에 의존하지 않습니다. 시작 구간은 2024-01이며 `catchup=False`, 일반 실행 `max_active_runs=1`입니다. 과거 월을 자동으로 한꺼번에 생성하지 않고 명시적인 백필로 처리합니다.

```sh
ACCOUNTING_RECONCILIATION_MODE=adjusted docker compose up --build -d
docker compose exec -T airflow airflow dags unpause accounting_quality_pipeline
```

원천 revision은 고정되어 있습니다. 현재 샘플은 2024-01~2025-12만 제공하므로 이후 월의 자동 실행은 `no_data`로 기록하고 발행하지 않습니다. 새로운 자료를 사용하려면 DAG 수동 실행 conf의 `revision`에 전체 40자리 commit SHA를 지정하거나 `ACCOUNTING_SOURCE_REVISION` 환경변수를 설정해야 합니다. 최신 revision을 임의로 따라가지 않습니다.

### 처리 구간과 과거 잔액

월별 실행은 원천 CSV를 수정하지 않고 `data/scoped/v2/<원본 manifest SHA256>/<YYYY-MM>/`에 검증 가능한 별도 스냅샷을 만듭니다. 대상 월말 이후의 전표·라인·시산표는 제외하되 이전 분개 전체는 유지하여 기초·누적잔액과 회계연도 손익을 계산합니다. 보고서에는 대상 월까지의 월별 이력이 포함됩니다. 원본 manifest SHA와 선택된 행을 매번 대조해 미래 행이 섞이거나 오래된 스냅샷이 사용되면 중단합니다. 상대 경로 기반 계보로 호스트와 컨테이너에서도 같은 검증을 수행합니다.

원천 범위 밖이면 `no_data`, 범위 안인데 월말 원천 시산표가 없으면 품질 실패입니다. `no_data`는 Airflow에서 정상 종료/건너뜀으로 처리되지만 발행 성공과 구분합니다. 모든 실행 유형을 enum 값으로 정규화하고 각 태스크가 요청 월을 다시 검사하므로, 스케줄·백필이 전체 기간 실행으로 바뀌는 것을 차단합니다.

CLI 월별 재실행:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m accounting.cli \
  --snapshot data/raw/b2f2d0890353438b4473f2c3b2d2b5e15a5132a0 \
  --period 2024-02 --reconciliation-mode adjusted
```

Airflow 수동 실행 conf 예시: `{"period":"2024-02","reconciliation_mode":"adjusted"}`. 수동 실행에서 period를 생략하면 기존 전체 기간 처리를 유지합니다. 스케줄/네이티브 백필은 `data_interval_start`의 한국시간 월을 사용합니다. 다른 월의 실행 이력과 혼동하지 않도록 수동 재처리에는 period를 명시하세요.

### 네이티브 백필

실행 중인 Airflow 컨테이너에서 다음 스크립트를 실행합니다. 기본은 **dry run**이며 대상 logical date만 확인합니다. `--execute`를 붙여 실제 백필을 생성합니다.

```sh
# 2024-01~02 대상만 확인
docker compose exec -T airflow python - \
  --from-month 2024-01 --to-month 2024-02 --mode adjusted < scripts/backfill.py

# 실제 생성. 기존 성공/실패가 있는 월도 새 실행으로 재처리
docker compose exec -T airflow python - \
  --from-month 2024-01 --to-month 2024-02 --mode adjusted \
  --reprocess completed --execute < scripts/backfill.py
```

`--reprocess none`은 기존 실행이 있는 logical date를 건너뛰고, `failed`는 실패한 월만 추가로 재처리하며, `completed`는 완료된 성공/실패 모두 재처리합니다. Airflow가 같은 run ID의 태스크를 재사용하는 경우에도 실행 세대와 이전 시도 기록을 보존합니다. `--reverse`로 최신 월부터 처리할 수 있습니다. 백필 동시 실행은 1로 제한합니다. 백필의 동시 실행 제한은 DAG 일반 실행 제한과 별개이므로 스케줄 실행과 겹칠 수 있으며, 스냅샷·이력 파일 잠금과 월별 발행 경로 분리를 사용합니다. 상세 의미는 [Airflow 3.4 백필 문서](https://airflow.apache.org/docs/apache-airflow/3.4.0/core-concepts/backfill.html)를 참고하세요.

### 월별 발행과 이력

```text
data/runs/index.html                      # 실행 이력 대시보드
data/runs/<run_id SHA256>.json             # 메타데이터·시도별 예외와 traceback
data/reports/<revision>/<month|full>/<key>/ # 실행별 보고서/검증 산출물
data/published/periods/<YYYY-MM>/latest.json
data/published/periods/<YYYY-MM>/<release>/ # 해당 월 발행본
data/published/latest.json                 # 전체 기간 발행본 전용
```

월별 발행 경로는 CLI뿐 아니라 발행 함수가 스냅샷에서 직접 판별합니다. 과거 월 백필은 다른 월과 전체 기간의 최신 포인터를 변경하지 않습니다. 원천과 기존 발행본은 그대로 유지합니다.

`data/runs/index.html`에서 대상 월·모드·성공/실패/no_data·실패 태스크·보고서를 확인할 수 있습니다. JSON에는 실행 종류, 실제 처리 구간, 시작/종료·소요 시간, 태스크별 시도 번호·상태·예외 타입·메시지·전체 traceback이 남습니다. 화면은 KST, JSON timestamp는 UTC입니다. 재시도와 clear 후 재실행은 이전 시도를 보존하고 최신 실행 세대의 상태만 최종 판정에 사용합니다. 프로세스가 강제 종료된 경우 상태는 마지막 기록이며, Airflow가 재개되어 최종 집계할 때 갱신됩니다.

마지막 `finalize_run`은 `all_done`으로 수행하지만 최신 실패와 발행 미완료를 다시 확인하여 예외를 발생시킵니다. 보고서 생성이나 이력 기록 성공이 앞선 실패를 성공으로 바꾸지 않습니다. 원천 범위 밖은 별도 no_data이며 이전 발행 결과를 성공으로 재사용하지 않습니다. Airflow UI의 해당 실행 ID에서 개별 태스크 로그와 clear/retry도 확인할 수 있습니다.

## 검증

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

테스트 72개는 수작업 예제의 기초/기말 잔액, 거래 없는 달, 회사 분리, 미전기 제외, 연도 경계, 재실행, 원천의 누락/중복/금액 차이, 월말 분개 증거 및 서로 상쇄되는 불균형 전표, 잘못된 금액, 누락 라인, 중복 ID, 참조/회사 오류, 스키마/체크섬 오류, 재실행 결과 일치 및 불완전 수집을 확인합니다. 재무제표 테스트는 수작업 손익·잔액, 손실, 차감계정, 인출, 회계연도 경계, 이익잉여금 중복 방지, 미매핑 계정, 마감 분개 차단과 의도적으로 변조한 재무제표 검출도 포함합니다. HTML/발행 테스트는 이스케이프·다운로드 링크, 정상 최종 발행·재실행, 위조된 통과 상태 차단, 체크섬 변경·필수 파일 누락, staging 중단 및 실패 시 이전 발행본 보존을 확인합니다. 보정 테스트는 원본 보존, 당월분만 반영한 잔액, 반복 실행, 누락 행 추가, 미설명·부분 분개·잘못된 모듈·키 중복 차단, 감사 파일 위조 탐지, strict/adjusted 모드 분리와 실제 발행까지 확인합니다. 현금흐름 테스트는 기초/기말 잔액, 현금 이체·비현금 제외, 투자·차입·인출, 명확한 분할 및 모호한 혼합 거래 차단, 회계연도 경계, 분류 누락, 원천 증거 누락, 금액 변조와 발행 차단을 확인합니다.

Airflow에서 실제 원천의 예상 차이까지 포함해 전체 DAG를 검증하려면 (strict 차단 및 최신 포인터 보존 확인):

```sh
docker compose run --rm -T -e ACCOUNTING_RECONCILIATION_MODE=strict airflow bash -c 'airflow db migrate && python' < scripts/check_airflow.py

# 15개 태스크 성공, 보정 대사 0건 및 실제 최종 묶음 확인
docker compose run --rm -T -e ACCOUNTING_RECONCILIATION_MODE=adjusted airflow bash -c 'airflow db migrate && python' < scripts/check_airflow.py
```

## 데이터 출처

- https://huggingface.co/datasets/mindweave/accounting-ledger-us
- 작성자: Mindweave Technologies
- 고정 revision: `b2f2d0890353438b4473f2c3b2d2b5e15a5132a0`
- 라이선스: [CC-BY-NC-4.0](https://creativecommons.org/licenses/by-nc/4.0/) — 출처 표시·비상업적 이용
- 데이터는 합성 데이터이며, 본 프로젝트는 원천 CSV를 변경하지 않습니다.

해당 revision의 실제 실행 결과: 회사 1개, 계정 60개, 전표 6,764건, 라인 17,027건, 제공 시산표 380행. 거래일 2024-01-01~2025-12-31. 전표 6,764건 모두 차대 일치, 검증 오류 0건.
