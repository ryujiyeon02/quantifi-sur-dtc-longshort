# 데이터 (저장소에 포함하지 않음)

엔진(`code/strategy_v8.py`)은 `data/processed/`에서 아래 파일을 읽습니다. 대부분 FnGuide DataGuide에서 받은 자료라 이용 약관상 재배포할 수 없어 저장소에 넣지 않았습니다. DataGuide 원천 엑셀을 `data/raw/`에 두고 `python code/build_processed_data.py`를 실행하면 같은 형식으로 만들어집니다.

wide 형식 parquet은 **행 = 일자(또는 월말), 열 = 6자리 종목코드**입니다.

| 파일 | 내용 | 형태 | 기간 | 원천 |
|---|---|---|---|---|
| `px_close_tr.parquet` | 수정주가(현금배당 포함), 원 | 일 × 종목 | 1999-01 ~ 2026-09 | DataGuide |
| `mktcap.parquet` | 시가총액, 백만원 | 일 × 종목 | 2012-01 ~ 2026-09 | DataGuide |
| `tradeval.parquet` | 거래대금, 원 | 일 × 종목 | 2012-01 ~ 2026-09 | DataGuide |
| `short_bal.parquet` | 차입공매도잔고비율, % | 일 × 종목 | 2012-01 ~ 2026-09 (값은 2016-06부터) | DataGuide |
| `eps_rev_fy1.parquet`, `eps_rev_fy2.parquet` | EPS 1개월 변화율(조정, 지배), % — FY1·FY2 | 월 × 종목 | 2006-01 ~ 2026-09 | DataGuide 컨센서스 |
| `fn_ig27_monthly.parquet` | FnGuide Industry Group 27 업종명 | 월 × 종목 | 2000-01 ~ 2026-09 | DataGuide |
| `index_px.parquet` | KOSPI200(`I.101`) 등 지수 종가 | 일 × 5 | 2012-01 ~ 2026-09 | DataGuide |
| `ff3.parquet` | HML·SMB 지수(종가지수 포인트), CP91 | 월 × 3 | 2012-01 ~ 2026-09 | DataGuide |
| `cd91.parquet` | CD 91일물 금리, 연 % | 월 × 1 | 2012-01 ~ 2026-09 | DataGuide (한국은행) |
| `k200_member_monthly.parquet` | 코스피200 구성종목 여부 (Y/N) | 월 × 종목 | 1999-01 ~ 2026-09 | DataGuide |
| `stock_names.csv` | 종목코드, 종목명 (보통주 판정용) | 3,987행 | — | DataGuide |
| `kospi200_kosdaq150_membership.csv` | 정기변경 이력 (rebal_date, ticker, index) | 15,258행 | 2015-06 ~ 2026-03 | KRX |
| `overheat.parquet` | 공매도 과열종목 지정일 플래그 (True/False) | 일 × 종목 | 2012-01 ~ 2026-09 | KRX 정보데이터시스템 |
| `naver_to_ig27_map.csv` | 네이버 업종 → IG27 대응표 (64개, 다수결) | 64행 | — | 네이버증권 + 자체 집계 |
| `industry_fill.csv` | IG27이 비어 있는 종목의 업종 채움표 | 166행 | — | 네이버증권 + 자체 집계 |

원천 엑셀(`data/raw/`)의 파일 이름과 시트 구성은 `code/build_processed_data.py` 상단에 있습니다.
