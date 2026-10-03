# -*- coding: utf-8 -*-
"""원천(data/raw) → 가공 데이터(data/processed) 변환

  python code/build_processed_data.py       # data/processed 에 저장
  python build_processed_data.py <폴더>     # 다른 폴더에 저장 (검증용)

DataGuide 엑셀 헤더 구조: 9행 종목코드(A######) / 10행 종목명 / 13행 아이템명 / 14행 주기 → 15행부터 데이터(A열 = 일자)
가공 형식: 행 = 일자, 열 = 6자리 종목코드. 엑셀 읽기는 openpyxl(read_only)이며 큰 파일은 수 분 걸린다.
"""
from __future__ import annotations

import json
import sys
from numbers import Real
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DG, KRX, NAVER = RAW / "dataguide", RAW / "krx", RAW / "naver"
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "data" / "processed"
HDR_CODE, HDR_NAME, HDR_ITEM, HDR_FREQ, DATA_START = 8, 9, 12, 13, 14   # 0부터 센 행 번호


def ticker(v) -> str:
    s = str(v).strip()
    return s[1:] if s.startswith("A") and len(s) == 7 else s


def num(v) -> float:
    return float(v) if isinstance(v, Real) and not isinstance(v, bool) else np.nan


def read_sheet(path: Path, sheet=0, keep=None, as_text=False):
    """DataGuide 시트 → (헤더 행들, DataFrame). keep(열 번호, 아이템명, 주기) → True 인 열만 읽는다."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[sheet] if isinstance(sheet, int) else wb[sheet]
    head, cols, idx, rows = [], None, [], []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i < DATA_START:
            head.append(r)
            if i == HDR_FREQ:
                cols = [j for j in range(1, len(head[HDR_CODE])) if head[HDR_CODE][j] not in (None, "", "nan")
                        and (keep is None or keep(j, head[HDR_ITEM][j], head[HDR_FREQ][j]))]
            continue
        d = pd.to_datetime(r[0], errors="coerce")
        if pd.isna(d): continue
        idx.append(d); rows.append([r[j] if as_text else num(r[j]) for j in cols])
    wb.close()
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="date"), columns=[ticker(head[HDR_CODE][j]) for j in cols])
    return head, df


def save(df: pd.DataFrame, name: str):
    OUT.mkdir(parents=True, exist_ok=True); df.to_parquet(OUT / f"{name}.parquet")
    print(f"[ok] {name:22s} {df.shape[0]}x{df.shape[1]}  {df.index.min():%Y-%m-%d} ~ {df.index.max():%Y-%m-%d}", flush=True)


def dedup(df: pd.DataFrame, keep_col="first") -> pd.DataFrame:
    return df.loc[~df.index.duplicated(keep="last"), ~df.columns.duplicated(keep=keep_col)].sort_index()


# 1) 현금배당 포함 수정종가 (체결·수익률·공분산·손절)
_, px = read_sheet(DG / "1999_2026_코스피코스닥_수정주가_현배포수정주가.xlsx",
                   keep=lambda j, item, f: str(item).strip() == "수정주가(현금배당포함)(원)")
save(dedup(px).astype("float32"), "px_close_tr")

# 2) 시가총액(백만원)·거래대금(원)
f = DG / "2012_2026_일별_코스피코스닥_시가총액_거래대금_비영업일제외.xlsx"
_, cap = read_sheet(f, "시가총액"); save(dedup(cap).astype("float64"), "mktcap")
_, tv = read_sheet(f, "거래대금"); save(dedup(tv).astype("float32"), "tradeval")
cal = tv.index.sort_values()                     # 과열종목 플래그의 일간 달력

# 3) 차입공매도잔고비율(%)
_, sb = read_sheet(DG / "2012_2026_일간_코스피코스닥_차입공매도수량_차입공매도잔고비율_대차거래잔고수량_비.xlsx", "차입공매도잔고비율")
save(dedup(sb).astype("float32"), "short_bal")

# 4) 지수(코스피200 등)·FF3 팩터(HML, SMB, 금리)
f = DG / "2012_2026_일간_코스피200_코스닥150_선물지수_FF3_시장경보지정여부_비영업일제외.xlsx"
_, ip = read_sheet(f, "코스피200,코스닥150,코스피,200선물,150선물"); save(dedup(ip).astype("float64"), "index_px")
_, ff = read_sheet(f, "FF3"); save(dedup(ff).astype("float64"), "ff3")

# 5) EPS 1개월 변화율 FY1·FY2 (월간)
EPS_ITEM = "EPS 변화율(1개월, 조정, 지배)(%)"
for fy in ("FY1", "FY2"):
    _, e = read_sheet(DG / "2006_2026_월간_BPS_EPS변화율(1개월)FY1FY2.xlsx",
                      keep=lambda j, item, fr, fy=fy: str(item).strip() == EPS_ITEM and str(fr).strip().upper() == fy)
    e = e.loc[:, [c.isdigit() and len(c) == 6 for c in e.columns]]      # 숫자 6자리 종목코드만 (영문 포함 신규 코드 형식 제외, 연구 단계 변환과 동일)
    e = e.loc[:, ~e.columns.duplicated(keep="last")].sort_index().astype("float32")
    e = e.loc[~e.index.to_period("M").duplicated(keep="last")]
    save(e, f"eps_rev_{fy.lower()}")

# 6) FnGuide Industry Group 27 (월간)
_, ig = read_sheet(DG / "코스피코스닥_산업분류_FnGuide Industry Group 27.xlsx", as_text=True)
save(ig.loc[:, ~ig.columns.duplicated()].sort_index(), "fn_ig27_monthly")

# 7) CD 91일물 금리(연 %, 월간)
raw = pd.read_excel(DG / "cd91.xlsx", sheet_name="cd91", header=None)
body = raw.iloc[DATA_START:, [0, 1]].dropna(); body.columns = ["date", "CD91"]; body["date"] = pd.to_datetime(body["date"])
cd = body.set_index("date")[["CD91"]].astype(float).sort_index(); save(cd[~cd.index.duplicated(keep="last")], "cd91")

# 8) 종목명 · 코스피200 월별 구성종목여부 (2026-04 이후 구성종목 보완용)
head, k2 = read_sheet(DG / "1999_2026_월별_코스피200_코스닥100_구성종목여부.xlsx", as_text=True)
codes = [str(c)[1:] for c in head[HDR_CODE][1:]]; names = [str(n) for n in head[HDR_NAME][1:]]
pd.DataFrame({"code": codes, "name": names}).to_csv(OUT / "stock_names.csv", index=False, encoding="utf-8-sig")
p = pd.PeriodIndex(k2.index, freq="M"); k2 = k2.loc[~p.duplicated(keep="last"), ~k2.columns.duplicated()]
save(k2, "k200_member_monthly")

# 9) KRX 공매도 과열종목 → 일자 × 종목 금지 플래그
oh = pd.concat([pd.read_csv(x, encoding="cp949", dtype={"종목코드": str}) for x in sorted(KRX.glob("data_4*_2026*.csv"))],
               ignore_index=True).drop_duplicates()
oh["s"] = pd.to_datetime(oh["공매도 거래제한일_시작일"], format="%Y/%m/%d")
oh["e"] = pd.to_datetime(oh["공매도 거래제한일_종료일"], format="%Y/%m/%d")
oh["ticker"] = oh["종목코드"].str.zfill(6)
flag = pd.DataFrame(False, index=cal, columns=sorted(oh.ticker.unique()))
for tk, s, e in zip(oh.ticker, oh.s, oh.e):
    m = (cal >= s) & (cal <= e)
    if m.any(): flag.loc[m, tk] = True
save(flag, "overheat")

# 10) KOSPI200·KOSDAQ150 정기변경 이력 (KRX) — 형식 그대로 복사
pd.read_csv(KRX / "kospi200_kosdaq150_membership.csv", dtype=str).to_csv(OUT / "kospi200_kosdaq150_membership.csv", index=False, encoding="utf-8-sig")

# 11) 업종 결측 채움: 네이버 업종 → IG27 대응표(IG27 이 있는 종목들의 다수결), 네이버가 없으면 같은 종목의 다른 달 IG27
T = pd.read_csv(NAVER / "naver_vs_ig27_train.csv", dtype={"code": str}).fillna({"naver_upjong": ""})
T = T[T.naver_upjong != ""]
ct = T.groupby(["naver_upjong", "ig27"]).size().rename("n").reset_index().sort_values(["naver_upjong", "n"], ascending=[True, False])
top = ct.groupby("naver_upjong").head(1).set_index("naver_upjong"); tot = T.groupby("naver_upjong").size()
MAP = pd.DataFrame({"ig27": top.ig27, "다수결 종목": top.n, "전체": tot}); MAP["일치율"] = MAP["다수결 종목"] / MAP["전체"]
MAP.to_csv(OUT / "naver_to_ig27_map.csv", encoding="utf-8-sig")
nv = pd.read_csv(NAVER / "naver_upjong_raw.csv", dtype={"code": str}).set_index("code")
I = ig.loc[:, ~ig.columns.duplicated()].sort_index().replace({"None": np.nan})
own = lambda c: (I[c].dropna().iloc[-1] if c in I.columns and I[c].notna().any() else np.nan)
rows = []
for c, r in nv.iterrows():
    nu = r.naver_upjong if isinstance(r.naver_upjong, str) else ""
    g = MAP["ig27"].get(nu, np.nan) if nu else np.nan; src = "네이버" if pd.notna(g) else ""
    o = own(c)
    if pd.isna(g) and pd.notna(o): g, src = o, "data 다른 달"
    rows.append(dict(code=c, 종목=r.naver_name if isinstance(r.naver_name, str) and r.naver_name else "", naver_upjong=nu, ig27=g,
                     출처=src, data_다른달=o, 일치=(pd.notna(o) and o == g) if pd.notna(g) else np.nan))
F = pd.DataFrame(rows)
# 네이버·IG27 어디에도 없는 1종목은 업종 성격으로 직접 판단 (설명서 2-1절)
F.loc[F.code == "220630", ["종목", "ig27", "출처"]] = ["(주)맘스터치앤컴퍼니", "소비자서비스", "판단: 외식업 = 네이버 '호텔,레스토랑,레저' → 소비자서비스 (상장폐지로 네이버 없음)"]
F.to_csv(OUT / "industry_fill.csv", encoding="utf-8-sig", index=False)
print("[ok] industry_fill", F["출처"].value_counts(dropna=False).to_dict())
print("DONE")
