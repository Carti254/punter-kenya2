"""Odds-only SportyBet accumulator builder. It does not estimate probabilities or outcomes."""
from __future__ import annotations
import math

def build_accumulator(candidates,target_odds,max_legs=20,beam_width=2500,max_candidates=500,ranking="odds",min_legs=2):
    target=float(target_odds)
    if not math.isfinite(target) or not 2.0 <= target <= 5.0:
        raise ValueError("Target combined odds must be between 2.00 and 5.00.")
    if not isinstance(max_legs,int) or not 1 <= max_legs <= 20: raise ValueError("max_legs must be 1–20.")
    if not isinstance(min_legs,int) or not 1 <= min_legs <= max_legs: raise ValueError("min_legs must be 1–max_legs.")
    upper=min(target*1.20,5.0)
    clean=[]
    for row in candidates or []:
        try: odd=float(row.get("odds"))
        except (TypeError,ValueError,OverflowError): continue
        if not math.isfinite(odd) or odd<=1: continue
        if str(row.get("bookmaker") or "").casefold().replace(" ","")!="sportybet": continue
        if not (row.get("eventId") or row.get("event_id")) or not (row.get("marketId") or row.get("market_id")) or not (row.get("outcomeId") or row.get("outcome_id")): continue
        if row.get("fixture_match_verified") is not True: continue
        home,away=str(row.get("home") or "").strip(),str(row.get("away") or "").strip()
        if not home or not away: continue
        day=str(row.get("day") or "")[:10]
        item=dict(row); item["odds"]=odd
        item["_fixture"]=(day,home.casefold(),away.casefold())
        clean.append(item)
    # One candidate per fixture/market/outcome; favour the shortest price only as a risk proxy.
    clean.sort(key=lambda x:(x["odds"],x["_fixture"],str(x.get("market")),str(x.get("selection"))))
    clean=clean[:max_candidates]
    if not clean:
        return {"selections":[],"combined_odds":1.0,"target_reached":False,"reason":"No executable SportyBet selections were found."}
    # Beam search prioritises the smallest number of legs, then the smallest target overshoot.
    states=[([],1.0,frozenset(),0)]
    complete=[]
    for _ in range(max_legs):
        expanded=[]
        for picks,product,used,start in states:
            for i in range(start,len(clean)):
                item=clean[i]
                if item["_fixture"] in used: continue
                new_product=product*item["odds"]
                if new_product>upper: continue
                new_picks=picks+[item]
                state=(new_picks,new_product,used|{item["_fixture"]},i+1)
                if len(new_picks)>=min_legs and target<=new_product<=upper: complete.append(state)
                if len(new_picks)<max_legs and new_product<target: expanded.append(state)
        if complete: break
        expanded.sort(key=lambda x:(abs(math.log(max(target,1.000001)/x[1])),len(x[0]),max(p["odds"] for p in x[0])))
        states=expanded[:beam_width]
        if not states: break
    if not complete:
        return {"selections":[],"combined_odds":1.0,"target_reached":False,
                "reason":"No combination of distinct fixtures reached the target using the available SportyBet prices."}
    best=min(complete,key=lambda x:(len(x[0]),x[1]-target,max(p["odds"] for p in x[0])))
    picks,product,_,_=best
    return {"selections":[{k:v for k,v in p.items() if not k.startswith("_")} for p in picks],
            "combined_odds":product,"target_reached":True,
            "selection_method":"Odds-only construction; no outcome probabilities or predictions.",
            "combined_probability":None,"expected_return_factor":None,"expected_roi":None,
            "independence_assumption":None,"reason":None}
