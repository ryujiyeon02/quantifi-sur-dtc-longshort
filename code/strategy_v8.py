# -*- coding: utf-8 -*-
"""QUANTIFI 3팀 — 이익수정 SUR 롱 / 커버일수 숏 롱숏 전략 (확정 설계 v8) 백테스트 엔진

입력은 모두 data/processed 에서 읽는다 (원천 data/raw → 가공은 code/build_processed_data.py).

규칙 요약
  신호일     매월 마지막 거래일. 체결은 신호 다음 거래일 종가(현금배당 포함 수정종가 = 총수익)
  유니버스   보통주(스팩·리츠·신주·인프라 제외) 중 시가총액 상위 30%, 최근 5거래일 연속 거래 + 21일 평균 거래대금 존재
  롱 신호    SUR = EPS 1개월 변화율(1~10월 FY1, 11~12월 FY2)을 횡단면 1/99% 윈저라이징한 뒤
             자기 과거 12개월 표준편차(최소 8개월, 당월 포함)로 나눈 값. 후보의 상위 10%(최소 5종목), 밴드 2배
  숏 신호    KOSPI200 구성종목(과열종목 제외) 중 커버일수 = 차입공매도잔고비율(3거래일 지연) ÷ 21일 거래회전율
             상위 10%(최소 5종목), 밴드 2배, 한 업종(FnGuide IG27) 최대 3종목. 롱 종목과 겹치면 숏 제외
  비중       (1−a)·최소분산 + a·√(21일 평균 거래대금) 비례, a = 롱 0.65 · 숏 0.70
             최소분산 = 252거래일 Ledoit-Wolf 상관 × 지수가중 변동성(반감 20일), 음수 비중 없음
             혼합 뒤 한 업종 비중 ≤ 30% (롱·숏 각각)
  노출       롱 100% / 숏 100% (gross 200%, net 0%). 공매도 금지기간에는 롱 100% · 숏 0%
  손절       숏 종목이 진입가 대비 +30% 이상 종가 마감하면 다음 거래일 종가에 청산
  비용       매매 편도 25bp(왕복 50bp), 대차 연 3%, 숏 매도대금은 CD91 이자 수령(리베이트)
             회전율 = |새 목표 비중 − 한 달 수익률로 떠내려간 직전 보유 비중|, 손절 종목은 청산 매수 1회 + 다음 달 0에서 출발
             손절 종목의 대차비용·리베이트는 실제 보유 거래일 비율만큼
  무위험     CD 91일물. Sharpe = (전략 − CD91) 기준
  기간       신호월 2016-12 ~ 2026-07 (보유 2017-01-02 ~ 2026-09-01, 116개월)
"""
from __future__ import annotations

from pathlib import Path
import warnings

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.covariance import LedoitWolf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
PQ = ROOT / "data" / "processed"

# ---------------------------------------------------------------- 규칙 (파라미터)
PARAMS = dict(
    START=pd.Timestamp("2016-12-01"), END=pd.Timestamp("2026-07-31"),   # 신호월 범위
    DATA_START=pd.Timestamp("2013-01-01"), DATA_END=pd.Timestamp("2026-08-31"),
    TOP_CAP=0.30,          # 시총 상위 30%
    SUR_WIN=12, SUR_MIN=8, WINSOR=0.01,
    LPCT=0.10, SPCT=0.10, NMIN=5, BAND_L=2.0, BAND_S=2.0, IND_MAX_N=3,
    LONG_SIG="sur",        # 롱 신호 (민감도용: "sur18", "sur24", "raw" — add_alt_signals 로 추가)
    REB=1, REB_OFF=0,      # 숏 교체 주기(개월)와 시작 달 (확정 = 매월)
    LONG_ONLY=False,       # True면 같은 롱을 숏 없이 보유 (비교용)
    IND_W_CAP_L=0.30, IND_W_CAP_S=0.30,
    BLEND_L=0.65, BLEND_S=0.70,
    COV_WIN=252, COV_MIN=126, EWMA_HL=20,
    STOP=0.30,
    COST_ONE_WAY=25 / 1e4, BORROW=0.03, REBATE=True,
    POS_TRACK=True,        # 회전율은 한 달 떠내려간 실제 보유(손절 종목은 0) 대비, 손절 종목 대차비용·리베이트는 보유일수만큼 (2026-09-30 검토 반영)
)
SHORT_BANS = [("2020-03-16", "2021-05-02"), ("2023-11-06", "2025-03-30")]   # 공매도 전면 금지


def pq(name: str) -> pd.DataFrame:
    return pd.read_parquet(PQ / f"{name}.parquet")


# ---------------------------------------------------------------- 공통 도구
def month_ends(cal: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(cal, index=cal)
    return pd.DatetimeIndex(s.groupby([cal.year, cal.month]).max().values)


def at_dates(frame: pd.DataFrame, dates, columns) -> pd.DataFrame:
    out = frame.copy(); out.index = pd.to_datetime(out.index)
    return out.sort_index().reindex(dates, method="ffill").reindex(columns=columns)


def monthly_frame(frame: pd.DataFrame, dates, columns) -> pd.DataFrame:
    """월간 원천을 같은 달끼리 붙인다 (월말 날짜가 영업일이라 날짜가 어긋날 수 있음)."""
    d = frame.copy(); d.index = pd.PeriodIndex(pd.to_datetime(d.index), freq="M")
    d = d[~d.index.duplicated(keep="last")].reindex(pd.PeriodIndex(dates, freq="M"))
    d.index = dates
    return d.reindex(columns=columns)


def ordered(score: pd.Series, ascending=False) -> list:
    """동점은 종목코드 순으로 고정해 재현성을 보장한다."""
    return list(score.sort_index().sort_values(ascending=ascending, kind="mergesort").index)


def with_band(previous, order, n, band):
    """기존 종목은 상위 n×band 안에 남는 동안 유지하고, 빈 자리는 상위순으로 채운다."""
    keep = set(order[: int(np.ceil(n * band))])
    sel = [x for x in previous if x in keep]
    sel += [x for x in order if x not in sel][: n - len(sel)]
    return sel[:n]


def band_with_cap(prev, order, n, band, groups, cap):
    """밴드 보유 + 업종 종목 수 상한: 기존 종목 우선 유지, 빈자리는 순위대로 채우되 업종당 cap 초과 금지."""
    keep = set(order[: int(np.ceil(n * band))]); sel, cnt = [], {}
    def ok_(x):
        g = groups.get(x); return g is None or pd.isna(g) or cnt.get(g, 0) < cap
    def add(x):
        g = groups.get(x); sel.append(x)
        if g is not None and not pd.isna(g): cnt[g] = cnt.get(g, 0) + 1
    for x in prev:
        if x in keep and ok_(x): add(x)
    for x in order:
        if len(sel) >= n: break
        if x not in sel and ok_(x): add(x)
    return sel[:n]


def is_common(code: str, name: str) -> bool:
    return (code.endswith("0") and "스팩" not in name and not name.endswith("리츠") and "신주" not in name
            and "인프라투융자" not in name and name != "맥쿼리인프라")


# ---------------------------------------------------------------- 수익률 (상장폐지 처리 포함)
def _delisting_exit(returns, entry, close, starts, valid, me):
    """보유 구간 중 거래가 끊긴 종목: 진입가가 있으면 구간 마지막 종가로 청산, 없으면 미체결(0%)."""
    need = close.reindex(me).gt(0) & ~np.isfinite(returns)
    log = []
    for i, date in enumerate(me):
        cols = need.columns[need.iloc[i].to_numpy()]
        if not len(cols): continue
        window = close.iloc[starts[valid[i]]:starts[valid[i] + 1]].reindex(columns=cols)
        window = window.where(window > 0)
        last = window.ffill().iloc[-1] if len(window) else pd.Series(np.nan, index=cols)
        ent = entry.loc[date, cols].where(entry.loc[date, cols] > 0)
        both = last.notna() & ent.notna()
        if both.any():
            t_ok = cols[both.to_numpy()]; r = last[t_ok].div(ent[t_ok]) - 1
            returns.loc[date, t_ok] = r.to_numpy(); log += [(date, t, float(r[t]), "partial_exit") for t in t_ok]
        none_ = ent.isna()
        if none_.any():
            t_no = cols[none_.to_numpy()]; returns.loc[date, t_no] = 0.0
            log += [(date, t, 0.0, "not_executable") for t in t_no]
    return returns, pd.DataFrame(log, columns=["date", "ticker", "adj_return", "kind"])


# ---------------------------------------------------------------- 데이터 준비
def load_data(p=PARAMS) -> dict:
    close = pq("px_close_tr")                               # 현금배당 포함 수정종가 (1999~)
    cal_all = close.index
    cols = pd.Index(sorted(close.columns))
    # 신호일(월말)과 보유수익률: 다음 거래일 종가 → 다음 달 신호 다음 거래일 종가
    me_all = month_ends(cal_all); starts = cal_all.searchsorted(me_all, side="right")
    valid = np.flatnonzero((me_all >= p["DATA_START"]) & (starts < len(cal_all)))
    valid = valid[valid + 1 < len(me_all)]; valid = valid[starts[valid + 1] < len(cal_all)]
    me = me_all[valid]
    entry = close.reindex(cal_all[starts[valid]]).set_axis(me)
    end = close.reindex(cal_all[starts[valid + 1]]).set_axis(me)
    returns = end.div(entry.where(entry > 0)) - 1
    returns, delist = _delisting_exit(returns, entry, close, starts, valid, me)
    dates = me[(me >= p["DATA_START"]) & (me <= p["DATA_END"])]
    returns = returns.reindex(index=dates, columns=cols)

    # 시가총액·거래대금·회전율
    mktcap = pq("mktcap"); tv = pq("tradeval").reindex(columns=cols).astype(float)
    cap_d = mktcap.reindex(columns=cols).reindex(cal_all).ffill()
    turn = pq("tradeval").reindex(columns=cols).rolling(21, min_periods=15).mean().div(cap_d.replace(0, np.nan)).reindex(dates, method="ffill")
    adv21 = tv.rolling(21, min_periods=15).mean()
    live5 = (tv.fillna(0) > 0).rolling(5).sum().eq(5)
    tradable = at_dates(live5.astype(float), dates, cols).eq(1.0) & at_dates(adv21, dates, cols).notna()

    # 보통주 시총 상위 30%
    names = pd.read_csv(PQ / "stock_names.csv", dtype=str, keep_default_na=False)
    NAME = dict(zip(names.code, names.name))
    common = pd.Series({c: is_common(c, NAME.get(c, "")) for c in cols})
    capm = mktcap.reindex(dates, method="ffill").reindex(columns=cols)
    capc = capm.where((capm > 0) & common.values)
    top = capc.rank(axis=1, pct=True).ge(1 - p["TOP_CAP"])

    # KOSPI200 구성종목 (2026-03까지 KRX 정기변경 이력, 이후는 DataGuide 월별 구성종목여부)
    mcsv = pd.read_csv(PQ / "kospi200_kosdaq150_membership.csv", dtype={"ticker": str})
    mcsv["rebal_date"] = pd.to_datetime(mcsv["rebal_date"]); mcsv["ticker"] = mcsv["ticker"].str.zfill(6)
    reb = sorted(mcsv.rebal_date.unique())
    snap = pd.DataFrame(np.nan, index=pd.DatetimeIndex(reb), columns=cols, dtype=object)
    for dd, g in mcsv.groupby("rebal_date"):
        g = g.drop_duplicates("ticker"); tk = [t for t in g.ticker if t in cols]
        snap.loc[dd, tk] = g.set_index("ticker")["index"].reindex(tk).values
    mem = snap.reindex(cal_all, method="ffill"); mem.loc[mem.index > snap.index.max(), :] = np.nan
    mem = mem.reindex(dates)
    k2 = pq("k200_member_monthly"); k2.index = pd.PeriodIndex(k2.index, freq="M")
    for t in dates[dates > pd.Timestamp("2026-03-31")]:
        y = k2.loc[pd.Period(t, "M")].reindex(mem.columns); row = pd.Series(np.nan, index=mem.columns, dtype=object)
        row[y.eq("Y").fillna(False).values] = "KOSPI200"; mem.loc[t] = row

    overheat = pq("overheat").reindex(index=dates, columns=cols).astype("boolean").fillna(False).astype(bool)
    ban = pd.Series(False, index=dates)
    for a, b in SHORT_BANS: ban |= (dates >= pd.Timestamp(a)) & (dates <= pd.Timestamp(b))

    # 업종: FnGuide IG27 (신호일 월), 결측은 네이버 업종 → IG27 대응표로 채움
    ind = monthly_frame(pq("fn_ig27_monthly"), dates, cols)
    fill = pd.read_csv(PQ / "industry_fill.csv", dtype={"code": str}).dropna(subset=["ig27"]).set_index("code")["ig27"]
    ind = ind.astype(object).where(ind.notna(), None)
    for c, g in fill.items():
        if c in ind.columns: ind[c] = ind[c].where(ind[c].notna(), g)

    # 숏 신호: 커버일수 백분위
    si_ratio = at_dates(pq("short_bal").shift(3), dates, cols).where(lambda x: x <= 100)
    dtc = si_ratio.div(turn.replace(0, np.nan)).rank(axis=1, pct=True).where(tradable)

    # 롱 신호: SUR
    f1 = monthly_frame(pq("eps_rev_fy1"), dates, cols).astype("float64")
    f2 = monthly_frame(pq("eps_rev_fy2"), dates, cols).astype("float64")
    raw = f1.where(~pd.Series(dates.month >= 11, index=dates), f2, axis=0)
    U = top & at_dates(live5.astype(float), dates, cols).eq(1.0)
    rw = raw.where(U)
    rw = rw.clip(rw.quantile(p["WINSOR"], axis=1), rw.quantile(1 - p["WINSOR"], axis=1), axis=0)
    sd = rw.rolling(p["SUR_WIN"], min_periods=p["SUR_MIN"]).std()
    sur = (rw / sd).where(tradable)                       # rw = 윈저라이징한 리비전, sd = 자기 과거 12개월 표준편차

    # 무위험수익률·시장·FF3 팩터
    rf = (pq("cd91")["CD91"].reindex(dates, method="ffill") / 100 / 12).fillna(0.0)
    ff = pq("ff3"); ff.columns = ["HML", "SMB", "CP91"][: ff.shape[1]]
    idx = pq("index_px")
    market = idx.iloc[:, 0].reindex(dates, method="ffill").pct_change().shift(-1)
    smb = ff["SMB"].reindex(dates, method="ffill").pct_change().shift(-1)
    hml = ff["HML"].reindex(dates, method="ffill").pct_change().shift(-1)
    sp = idx["I.101"].dropna(); scal = sp.index
    def _nx(t):
        j = scal.searchsorted(t, side="right"); return scal[j] if j < len(scal) else pd.NaT
    def _span(i):                                           # KOSPI200: 전략과 같은 보유기간
        a = _nx(dates[i]); b = _nx(dates[i + 1]) if i + 1 < len(dates) else _nx(dates[i] + pd.offsets.MonthEnd(1))
        return sp.loc[b] / sp.loc[a] - 1 if pd.notna(a) and pd.notna(b) else np.nan
    k200 = pd.Series({t: _span(i) for i, t in enumerate(dates)})

    # 일간: 체결 달력(2012~), 종가, 거래대금
    cal = cal_all[cal_all >= tv.index.min()]
    pc = close.reindex(index=cal, columns=cols)
    dr = pc.pct_change(fill_method=None)
    ewv = (dr ** 2).ewm(halflife=p["EWMA_HL"], min_periods=p["EWMA_HL"]).mean()
    return dict(dates=dates, cols=cols, returns=returns, delist=delist, top=top, mem=mem, overheat=overheat, ban=ban,
                ind=ind, dtc=dtc, sur=sur, raw=raw.where(U), rw=rw, sd=sd, rf=rf, market=market, smb=smb, hml=hml, k200=k200,
                cal=cal, pc=pc, dr=dr, ewv=ewv, adv21=adv21, capm=capm, capc=capc, tradable=tradable, si_ratio=si_ratio, NAME=NAME,
                mktcap=mktcap, close_all=close)


# ---------------------------------------------------------------- 비중
def adv_at(D, t):
    j = D["adv21"].index.searchsorted(t, side="right") - 1; return D["adv21"].iloc[j]


def min_var(D, t, names, p=PARAMS, log=None):
    """252거래일 Ledoit-Wolf 상관 × 지수가중 변동성(반감 20일) 최소분산, 음수 비중 없음."""
    eq = pd.Series(1 / len(names), index=names); dr = D["dr"]
    j = dr.index.searchsorted(t, side="right")
    W = dr.iloc[max(0, j - p["COV_WIN"]):j].reindex(columns=names); W = W.loc[:, W.notna().sum() >= p["COV_MIN"]]
    if W.shape[1] < 5: return eq
    X = W.fillna(0).values; lw = LedoitWolf().fit(X - X.mean(0))
    if log is not None: log.append(lw.shrinkage_)
    S = lw.covariance_; sd = np.sqrt(np.maximum(np.diag(S), 1e-12)); Cr = S / np.outer(sd, sd)   # 분산 0 방어
    v = D["ewv"].iloc[j - 1].reindex(W.columns).values; vol = np.where(np.isfinite(v) & (v > 0), np.sqrt(v), sd)
    S = Cr * np.outer(vol, vol)
    try: w = np.linalg.solve(S, np.ones(W.shape[1]))
    except np.linalg.LinAlgError: w = np.linalg.pinv(S) @ np.ones(W.shape[1])                 # 특이행렬 방어
    w = np.clip(w, 0, None)
    if w.sum() <= 0: return eq
    x = pd.Series(w / w.sum(), index=W.columns).reindex(names); x = x.fillna(x.mean()); return x / x.sum()


def liq_w(D, t, names):
    z = np.sqrt(adv_at(D, t).reindex(names).fillna(0.0).clip(lower=0.0))
    return z / z.sum() if z.sum() > 0 else pd.Series(1 / len(names), index=names)


def ind_cap(D, w, t, cap):
    """한 업종 비중 ≤ cap. 초과분은 여유 업종 종목에 원래 비중 비율대로 재배분 (미분류는 한 업종)."""
    if cap is None or len(w) == 0: return w
    g = D["ind"].loc[t].reindex(w.index).fillna("미분류"); cap = max(cap, 1.0 / g.nunique()); w0 = w.copy(); w = w.copy()
    for _ in range(100):
        gs = w.groupby(g).sum(); over = gs[gs > cap + 1e-10]
        if over.empty: break
        ex = 0.0
        for k in over.index:
            m = g.eq(k); ex += w[m].sum() - cap; w[m] = w[m] * cap / w[m].sum()
        gs = w.groupby(g).sum(); room = g.isin(gs[gs < cap - 1e-10].index)
        rec = room & (w0 > 0); base = w0[rec] if rec.any() else pd.Series(1.0, index=w.index[room])
        w[base.index] = w[base.index] + ex * base / base.sum()
    return w / w.sum()


def weights(D, t, names, side, p=PARAMS, log=None):
    a = p["BLEND_L"] if side == "long" else p["BLEND_S"]
    cap = p["IND_W_CAP_L"] if side == "long" else p["IND_W_CAP_S"]
    w = (1 - a) * min_var(D, t, names, p, log).reindex(names).fillna(0.0) + a * liq_w(D, t, names)
    return ind_cap(D, w / w.sum(), t, cap)


# ---------------------------------------------------------------- 백테스트
def run_backtest(D: dict, p=PARAMS):
    dates = D["dates"]; DL = list(dates[dates >= p["START"]]); cal = D["cal"]; pc = D["pc"]
    def nxt(t):
        j = cal.searchsorted(t, side="right"); return j if j < len(cal) else None
    c1 = p["COST_ONE_WAY"]; prev_l, prev_s = [], []; pwl, pws = pd.Series(dtype=float), pd.Series(dtype=float)
    rows, hold, events, shrink = [], {}, [], []
    for i, t in enumerate(DL):
        # 1) 롱: SUR 상위 10%, 밴드
        lo = ordered(D[p["LONG_SIG"]].loc[t].where(D["top"].loc[t]).dropna(), ascending=False)
        if len(lo) < p["NMIN"]: continue
        longs = with_band(prev_l, lo, max(p["NMIN"], int(round(p["LPCT"] * len(lo)))), p["BAND_L"])
        # 2) 숏: KOSPI200·과열 아님 중 커버일수 상위 10%, 밴드, 업종 최대 3종목
        ban = bool(D["ban"].loc[t])
        ok = D["mem"].loc[t].eq("KOSPI200") & ~D["overheat"].loc[t]
        if ban: ok[:] = False
        cand = D["dtc"].loc[t].where(ok).dropna()
        so = [x for x in ordered(cand, ascending=False) if x not in set(longs)]
        ns_ = max(p["NMIN"], int(round(p["SPCT"] * len(so)))) if so else 0
        if p["REB"] == 1 or (i % p["REB"]) == p["REB_OFF"] or not prev_s or ban:
            shorts = band_with_cap(prev_s, so, ns_, p["BAND_S"], D["ind"].loc[t].to_dict(), p["IND_MAX_N"]) if so else []
        else:                                         # 교체 달이 아니면 기존 숏 중 여전히 숏 가능한 종목만 유지
            shorts = [x for x in prev_s if bool(ok.get(x, False)) and x not in set(longs)]
        # 3) 노출·비중
        L, S = (1.0, 0.0) if (ban or not shorts or p["LONG_ONLY"]) else (1.0, 1.0)
        wl = weights(D, t, longs, "long", p, shrink)
        ws = weights(D, t, shorts, "short", p, shrink) if shorts and S > 0 else pd.Series(dtype=float)
        Le = L * float(wl.sum()); Se = S * float(ws.sum()) if len(ws) else 0.0
        ret = D["returns"].loc[t].copy()
        for side, nm in (("long", list(wl.index)), ("short", list(ws.index))):
            bad = [x for x in nm if not np.isfinite(ret.get(x, np.nan))]
            if bad: raise ValueError(f"{t:%Y-%m-%d} {side}: 사후 가격 미확인 {bad}")
        nl = wl / wl.sum(); nsw = ws / ws.sum() if len(ws) else ws
        rl = ret.reindex(nl.index).copy(); rs = ret.reindex(nsw.index).copy() if len(nsw) else pd.Series(dtype=float)
        # 4) 숏 손절: 진입가 대비 +30% 종가 → 다음 거래일 종가 청산
        extra_to, n_stop, frac = 0.0, 0, {}          # frac: 손절 종목의 보유 거래일 비율
        j1 = nxt(t); j2 = nxt(DL[i + 1]) if i + 1 < len(DL) else nxt(t + pd.offsets.MonthEnd(1))
        if len(nsw) and j1 is not None and j2 is not None and j2 > j1 and p["STOP"]:
            p1 = pc.iloc[j1].reindex(nsw.index)
            path = pc.iloc[j1 + 1:j2].reindex(columns=nsw.index).div(p1)
            for c in nsw.index:
                h = np.flatnonzero(np.nan_to_num(path[c].values, nan=0) >= 1 + p["STOP"])
                if len(h) and h[0] + 2 < (j2 - j1):
                    k = h[0]; ex = pc.iloc[j1 + k + 2].get(c, np.nan); p0 = p1.get(c, np.nan)
                    if pd.notna(ex) and pd.notna(p0) and p0 > 0:
                        r_exit = ex / p0 - 1; rs[c] = r_exit; n_stop += 1
                        if p["POS_TRACK"]: extra_to += nsw[c] * Se * (1 + r_exit); frac[c] = (k + 2) / (j2 - j1)   # 청산 매수 1회 (재진입은 다음 달 회전율)
                        else: extra_to += 2 * nsw[c] * Se
                        events.append(dict(date=t, trig_day=cal[j1 + k + 1], exit_day=cal[j1 + k + 2], code=c, name=D["NAME"].get(c, c),
                                           w_nav=nsw[c] * Se, r_exit=r_exit, month_end_ret=pc.iloc[j2 - 1].get(c, np.nan) / p0 - 1))
        def wret(w, r):
            if w.empty: return 0.0
            m = r.reindex(w.index).notna()
            return float((w[m] * r.reindex(w.index)[m]).sum() / w[m].sum()) if m.any() else 0.0
        lr = wret(nl, rl); sr = wret(nsw, rs) if len(nsw) else 0.0
        # 5) 비용·수익
        WL = nl * Le; WS = nsw * Se if len(nsw) else pd.Series(dtype=float); x = D["rf"].get(t, 0.0)
        to = WL.sub(pwl, fill_value=0).abs().sum() + WS.sub(pws, fill_value=0).abs().sum() + extra_to
        Sh = float((WS * pd.Series(frac).reindex(WS.index).fillna(1.0)).sum()) if len(WS) else 0.0   # 보유일수 가중 숏 노출
        tc = to * c1; bc = Sh * p["BORROW"] / 12; ex_pre = Le * (lr - x) - Se * sr
        rebate = x * Sh if p["REBATE"] else 0.0
        rows.append(dict(date=t, ex=ex_pre - tc - bc + rebate, L=Le, S=Se, long=lr, short=sr, to=to, tc=tc, bc=bc,
                         rebate=rebate, S_hold=Sh, rf=x, mkt=D["k200"].get(t, np.nan), short_pnl=-Se * sr, n_long=len(longs),
                         n_short=len(shorts), n_stop=n_stop, ban=ban))
        hold[t] = (WL.copy(), WS.copy())
        if p["POS_TRACK"]:                            # 다음 리밸런싱 직전 실제 보유: 한 달 수익률로 떠내려간 비중, 손절 종목은 0
            g = 1 + rows[-1]["ex"] + x
            pwl_n = WL * (1 + rl.reindex(WL.index).fillna(0)) / g
            pws_n = (WS * (1 + rs.reindex(WS.index).fillna(0)) / g).drop(list(frac)) if len(WS) else WS
        else: pwl_n, pws_n = WL, WS
        prev_l, prev_s, pwl, pws = longs, shorts, pwl_n, pws_n
    M = pd.DataFrame(rows).set_index("date").loc[:p["END"]]
    hold = {k: v for k, v in hold.items() if k <= p["END"]}
    ev = pd.DataFrame(events); ev = ev[ev.date <= p["END"]] if len(ev) else ev
    return M, hold, ev, shrink


# ---------------------------------------------------------------- 성과
def perf(tot: pd.Series, rf: pd.Series) -> dict:
    n = (1 + tot).cumprod(); e = tot - rf; ve = e.std() * np.sqrt(12); dd = n / n.cummax() - 1
    end = dd.idxmin(); pk = n.loc[:end].idxmax()
    return {"누적": n.iloc[-1] - 1, "CAGR": n.iloc[-1] ** (12 / len(tot)) - 1, "연변동성": tot.std() * np.sqrt(12),
            "Sharpe(CD91차감)": e.mean() * 12 / ve, "Sortino": e.mean() * 12 / (np.sqrt((e.clip(upper=0) ** 2).mean()) * np.sqrt(12)),
            "MDD": dd.min(), "MDD구간(신호월)": f"{pk:%Y-%m}→{end:%Y-%m}", "최악의달": tot.min(), "최고의달": tot.max(),
            "월승률(>0)": (tot > 0).mean(), "Calmar": (n.iloc[-1] ** (12 / len(tot)) - 1) / abs(dd.min()), "개월": len(tot)}


def market_all_cw_tr(D) -> pd.Series:
    """코스피+코스닥 전 종목 시가총액가중 총수익 (전략과 같은 보유기간)."""
    cap = D["mktcap"].astype(float); px = D["close_all"].astype(float); cal = px.index; pf = px.ffill(limit=10); out = {}
    for t in D["dates"]:
        a = cal.searchsorted(t, side="right")
        nx = (t + pd.offsets.MonthEnd(0)) + pd.offsets.MonthEnd(1); b = cal.searchsorted(cal[cal <= nx][-1], side="right")
        if a >= len(cal) or b >= len(cal): continue
        w = cap.loc[:t].iloc[-1]; p0 = px.iloc[a]; p1 = pf.iloc[b]
        ok = (w > 0) & p0.notna() & (p0 > 0); r = (p1 / p0 - 1).where(ok).fillna(-0.0); ww = w.where(ok).fillna(0)
        out[t] = float((ww * r).sum() / ww.sum())
    return pd.Series(out, name="MKT_all")


def factor_regression(y: pd.Series, X: pd.DataFrame):
    Dd = pd.concat([y.rename("y"), X], axis=1).dropna()
    f = sm.OLS(Dd.y, sm.add_constant(Dd.drop(columns="y"))).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
    return f, len(Dd)


def fill_rate(D, M, H, aum, days, part=0.10):
    """매매 체결률: 매달 주문(목표 − 한 달 떠내려간 기존 보유) 중 21일 평균 거래대금 × part × days 안에 체결되는 금액 비율 (전 기간 금액 가중).
    aum = 롱·숏 각 다리 운용 금액(원)."""
    trd = []; prev = {"롱": pd.Series(dtype=float), "숏": pd.Series(dtype=float)}; pt = None
    for t in M.index:
        lim = adv_at(D, t) * part * days; WL, WS = H[t]
        for k, leg, w in (("롱", M.L.loc[t], WL), ("숏", M.S.loc[t], WS)):
            tgt = (w * aum * leg) if (leg > 0 and len(w)) else pd.Series(dtype=float); tgt = tgt[tgt > 0]; old = prev[k]
            if len(old) and pt is not None:
                rr = D["returns"].loc[pt].reindex(old.index).fillna(0); v = old * (1 + rr); old = v / v.sum() * old.sum()
            nm = tgt.index.union(old.index); tr = (tgt.reindex(nm).fillna(0) - old.reindex(nm).fillna(0)).abs(); tr = tr[tr > 1e6]
            if len(tr): trd.append((t, k, np.minimum(tr, lim.reindex(tr.index).fillna(0)).sum(), tr.sum()))
            prev[k] = tgt
        pt = t
    X = pd.DataFrame(trd, columns=["date", "leg", "fill", "amt"])
    out = {"전체": X.fill.sum() / X.amt.sum()}
    for k in ("롱", "숏"):
        x = X[X.leg == k]; out[k] = x.fill.sum() / x.amt.sum()
    return out


# ---------------------------------------------------------------- 민감도용 도구
def add_alt_signals(D, p=PARAMS):
    """롱 신호 대안: SUR 창 18·24개월, 표준화하지 않은 원 리비전."""
    D = dict(D)
    for n in (18, 24): D[f"sur{n}"] = (D["rw"] / D["rw"].rolling(n, min_periods=max(8, n // 2)).std()).where(D["tradable"])
    D["raw_sig"] = D["raw"].where(D["tradable"])
    return D


def with_halflife(D, hl):
    """변동성 지수가중 반감기를 바꾼 데이터 (나머지는 공유)."""
    D = dict(D); D["ewv"] = (D["dr"] ** 2).ewm(halflife=hl, min_periods=min(hl, 20)).mean(); return D


def sharpe(M):
    return float(M.ex.mean() * 12 / (M.ex.std() * np.sqrt(12)))


# ---------------------------------------------------------------- 신호 검증 (5분위 · 시가총액 교차 히트맵)
def _hac_t(x, lags=3):
    x = pd.Series(x).dropna(); f = sm.OLS(x.values, np.ones((len(x), 1))).fit(cov_type="HAC", cov_kwds={"maxlags": lags}); return f.tvalues[0]


def quintile_test(D, sig, months, universe=None, q=5, min_n=50):
    """매달 신호로 q분위를 나눠 다음 보유기간 동일가중 수익률을 본다. universe(t) → 종목별 True/False (없으면 신호가 있는 전 종목)."""
    rec = {}
    for t in months:
        x = sig.loc[t]
        if universe is not None: x = x.where(universe(t))
        x = x.dropna(); r = D["returns"].loc[t].reindex(x.index); ok = r.notna(); x, r = x[ok], r[ok]
        if len(x) < min_n: continue
        g = pd.qcut(x.rank(method="first"), q, labels=False); rec[t] = {f"Q{i + 1}": r[g == i].mean() for i in range(q)}
    G = pd.DataFrame(rec).T; G[f"Q{q}-Q1"] = G[f"Q{q}"] - G["Q1"]
    return pd.DataFrame({c: {"CAGR": (1 + G[c]).prod() ** (12 / len(G)) - 1, "산술 연율": G[c].mean() * 12, "t": _hac_t(G[c]), "개월": len(G)} for c in G.columns}).T


def size_grid(D, sig, months, universe, q=5):
    """신호 q분위 × 시가총액 q분위 (독립 정렬) 셀별 다음 보유기간 동일가중 수익률의 CAGR. 행 = 시총(소형→대형), 열 = 신호(낮음→높음)."""
    rows = {}
    for t in months:
        x = sig.loc[t].where(universe(t)).dropna(); c = D["capc"].loc[t].reindex(x.index).dropna(); x = x.reindex(c.index)
        r = D["returns"].loc[t].reindex(x.index); ok = r.notna(); x, c, r = x[ok], c[ok], r[ok]
        if len(x) < q * q * 4: continue
        qc = pd.qcut(c.rank(method="first"), q, labels=False); qs = pd.qcut(x.rank(method="first"), q, labels=False)
        rows[t] = {(i, j): r[(qc == i) & (qs == j)].mean() for i in range(q) for j in range(q)}
    P = pd.DataFrame(rows).T; H = np.full((q, q), np.nan)
    for i in range(q):
        for j in range(q):
            v = P[(i, j)].astype(float).dropna(); H[i, j] = (1 + v).prod() ** (12 / len(v)) - 1
    return pd.DataFrame(H, index=[f"시총Q{i + 1}" for i in range(q)], columns=[f"신호Q{j + 1}" for j in range(q)])
