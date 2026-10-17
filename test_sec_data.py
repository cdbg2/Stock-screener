from datetime import date
from types import SimpleNamespace
import json

import pytest

import sec_data
from sec_data import SecClient, SecAccessError, points, extract_financials


def item(year, value, **kw):
    result = dict(start=f"{year}-01-01",end=f"{year}-12-31",val=value,
                  filed=f"{year+1}-02-01",fy=2024,form="10-K",accn=f"report-{year}")
    result.update(kw)
    return result


def payload():
    gaap = {
        "NetIncomeLoss":{"units":{"USD":[item(2024,100_000_000)]}},
        "OperatingIncomeLoss":{"units":{"USD":[item(2024,200_000_000)]}},
        "InterestExpense":{"units":{"USD":[item(2024,10_000_000)]}},
        "StockholdersEquity":{"units":{"USD":[item(2024,500_000_000,start="")]}},
        "EarningsPerShareDiluted":{"units":{"USD/shares":[item(y,y-2018) for y in range(2019,2025)]}},
    }
    facts = {"entityName":"Test","facts":{"us-gaap":gaap}}
    subs = {"filings":{"recent":{"form":["10-K"],"reportDate":["2024-12-31"],
                                  "filingDate":["2025-02-01"],"accessionNumber":["2024-report"]}}}
    return facts,subs


def extract(facts,subs):
    return extract_financials(facts,subs,today=date(2025,10,17))


def test_annual_period_not_filing_fy_and_quarters_excluded():
    f,s=payload();values=f["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    values.extend([item(2024,999,start="2024-10-01"),item(2024,888,form="10-Q")])
    assert extract(f,s)["net_income"]==100_000_000
    assert extract(f,s)["eps"]==[1,2,3,4,5,6]


def test_latest_restatement_wins_and_usd_units_only():
    f,s=payload();concept=f["facts"]["us-gaap"]["NetIncomeLoss"]
    concept["units"]["USD"].append(item(2024,110_000_000,filed="2025-03-01",form="10-K/A"))
    concept["units"]["EUR"]=[item(2024,999999999)]
    assert extract(f,s)["net_income"]==110_000_000


def test_equity_and_interest_must_match_latest_income_period():
    f,s=payload();g=f["facts"]["us-gaap"]
    g["StockholdersEquity"]["units"]["USD"]=[item(2023,123,start="")]
    g["InterestExpense"]["units"]["USD"]=[item(2023,123)]
    m=extract(f,s)
    assert m["equity"] is None and m["interest_expense"] is None


def test_interest_start_date_must_match_income():
    f,s=payload();f["facts"]["us-gaap"]["InterestExpense"]["units"]["USD"][0]["start"]="2023-12-01"
    assert extract(f,s)["interest_expense"] is None


def test_newer_annual_filing_prevents_using_stale_facts():
    f,s=payload();s["filings"]["recent"]["reportDate"]=["2025-06-30"]
    m=extract(f,s)
    assert m["net_income"] is None
    assert "latest annual" in m["issues"][0]


def test_stale_and_ifrs_are_explicit():
    f,s=payload()
    assert "stale" in extract_financials(f,s,today=date(2027,1,1))["issues"][0]
    f["facts"]={"ifrs-full":{}}
    assert "IFRS" in extract(f,s)["issues"][0]


def test_diluted_and_basic_eps_not_mixed_to_fill_holes():
    f,s=payload();g=f["facts"]["us-gaap"]
    g["EarningsPerShareDiluted"]["units"]["USD/shares"].pop(2)
    g["EarningsPerShareBasic"]={"units":{"USD/shares":[item(y,y-2018) for y in range(2019,2025)]}}
    assert extract(f,s)["eps"]==[]


def test_basic_eps_used_when_no_current_diluted_eps():
    f,s=payload();g=f["facts"]["us-gaap"]
    g["EarningsPerShareBasic"]=g.pop("EarningsPerShareDiluted")
    assert extract(f,s)["eps"]==[1,2,3,4,5,6]


def test_eps_gap_not_bridged_with_older_year():
    f,s=payload();values=f["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
    values.pop(2);values.insert(0,item(2018,0))
    assert extract(f,s)["eps"]==[]


def test_case_sensitive_standard_interest_tag():
    f,s=payload();g=f["facts"]["us-gaap"]
    g["InterestExpenseNonoperating"]=g.pop("InterestExpense")
    m=extract(f,s)
    assert m["interest_expense"]==10_000_000
    assert m["tags"]["interest_expense"]["tag"]=="InterestExpenseNonoperating"


def test_non_calendar_52_week_fiscal_years():
    f,s=payload();g=f["facts"]["us-gaap"]
    ends=["2019-09-28","2020-09-26","2021-09-25","2022-09-24","2023-09-30","2024-09-28"]
    starts=["2018-09-30","2019-09-29","2020-09-27","2021-09-26","2022-09-25","2023-10-01"]
    for tag in ["NetIncomeLoss","OperatingIncomeLoss","InterestExpense"]:
        g[tag]["units"]["USD"][0].update(start=starts[-1],end=ends[-1])
    g["StockholdersEquity"]["units"]["USD"][0]["end"]=ends[-1]
    for i,v in enumerate(g["EarningsPerShareDiluted"]["units"]["USD/shares"]):
        v.update(start=starts[i],end=ends[i])
    s["filings"]["recent"]["reportDate"]=[ends[-1]]
    assert extract(f,s)["eps"]==[1,2,3,4,5,6]


def test_zero_eps_is_not_missing():
    f,s=payload();f["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][0]["val"]=0
    assert extract(f,s)["eps"][0]==0


def response(code,data=None,headers=None):
    return SimpleNamespace(status_code=code,headers=headers or {},
                           raise_for_status=lambda:None,json=lambda:data)


def test_rate_limited_request_retries_and_caches(tmp_path,monkeypatch):
    client=SecClient(cache_dir=tmp_path);calls=[];sleeps=[]
    answers=iter([response(429,headers={"Retry-After":"1"}),response(200,{"ok":True})])
    monkeypatch.setattr(client.session,"get",lambda *a,**kw:calls.append(a) or next(answers))
    monkeypatch.setattr(sec_data.time,"sleep",sleeps.append)
    assert client.get_json("https://data.sec.gov/example","sample")=={"ok":True}
    assert client.get_json("https://data.sec.gov/example","sample")=={"ok":True}
    assert len(calls)==2 and 1 in sleeps


def test_sec_access_denied_stops_immediately(tmp_path,monkeypatch):
    client=SecClient(cache_dir=tmp_path)
    monkeypatch.setattr(client.session,"get",lambda *a,**kw:response(403))
    with pytest.raises(SecAccessError,match="403"):
        client.get_json("https://data.sec.gov/example","sample")


def test_exhausted_retries_are_not_saved(tmp_path,monkeypatch):
    client=SecClient(cache_dir=tmp_path)
    monkeypatch.setattr(client.session,"get",lambda *a,**kw:response(503))
    monkeypatch.setattr(sec_data.time,"sleep",lambda *a:None)
    with pytest.raises(SecAccessError,match="503"):
        client.get_json("https://data.sec.gov/example","sample")
    assert not (tmp_path/"sample.json").exists()


def test_stale_daily_cache_is_refreshed(tmp_path,monkeypatch):
    client=SecClient(cache_dir=tmp_path)
    (tmp_path/"sample.json").write_text(json.dumps({"day":"2000-01-01","data":{"old":True}}))
    monkeypatch.setattr(client.session,"get",lambda *a,**kw:response(200,{"new":True}))
    assert client.get_json("https://data.sec.gov/example","sample")=={"new":True}


def test_share_class_alias_and_issuer_requests_are_reused(tmp_path,monkeypatch):
    client=SecClient(cache_dir=tmp_path);f,s=payload();calls=[]
    def get(url,key):
        calls.append(key)
        if key=="tickers":return {"0":{"ticker":"BRK-B","cik_str":1067983}}
        return f if key.startswith("facts-") else s
    monkeypatch.setattr(client,"get_json",get)
    assert client.financials("BRK.B") is client.financials("BRK-B")
    assert len(calls)==3


def test_stock_split_with_unrestated_old_eps_is_not_compared():
    f,s=payload();f["facts"]["us-gaap"]["StockholdersEquityNoteStockSplitConversionRatio1"]={
        "units":{"pure":[{"end":"2023-06-30","val":10,"filed":"2023-08-01"}]}}
    m=extract(f,s)
    assert m["eps"]==[]
    assert any("stock split" in issue for issue in m["issues"])
    for v in f["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]:
        v["filed"]="2025-02-01"
    assert extract(f,s)["eps"]==[1,2,3,4,5,6]
