"""Read-only recent public-post search using the official X API v2.

Requires X_BEARER_TOKEN supplied privately through the deployment environment.
Posts are leads for human verification, not confirmed team news.
"""
import os
import requests


API = "https://api.x.com/2/tweets/search/recent"


def search_match_posts(home, away, limit=20, token=None, session=requests):
    bearer = token or os.getenv("X_BEARER_TOKEN")
    if not bearer:
        raise RuntimeError("X research is not configured. Add X_BEARER_TOKEN privately to the deployment environment.")
    home, away = str(home).strip(), str(away).strip()
    if not home or not away:
        raise ValueError("Both team names are required.")
    query = f'("{home}" OR "{away}") (injury OR injured OR suspended OR lineup OR squad OR manager OR training OR transfer) -is:retweet lang:en'
    response = session.get(
        API,
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        params={
            "query": query,
            "max_results": max(10, min(int(limit), 100)),
            "tweet.fields": "created_at,lang,public_metrics,author_id,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,verified,public_metrics",
        },
        timeout=(5, 20),
    )
    if response.status_code == 401:
        raise RuntimeError("X rejected the bearer token. Check X API access and token validity.")
    if response.status_code == 403:
        raise RuntimeError("X API access is not enabled for recent-post search on this account.")
    response.raise_for_status()
    payload = response.json()
    includes = payload.get("includes") or {}
    users = {str(u.get("id")): u for u in includes.get("users") or []}
    posts = []
    for item in payload.get("data") or []:
        user = users.get(str(item.get("author_id")), {})
        metrics = item.get("public_metrics") or {}
        posts.append({
            "text": item.get("text", ""),
            "created_at": item.get("created_at"),
            "username": user.get("username"),
            "verified": bool(user.get("verified")),
            "likes": int(metrics.get("like_count") or 0),
            "reposts": int(metrics.get("retweet_count") or 0),
            "replies": int(metrics.get("reply_count") or 0),
            "url": f"https://x.com/{user.get('username')}/status/{item.get('id')}" if user.get("username") else None,
        })
    posts.sort(key=lambda x: (x["verified"], x["likes"] + 2*x["reposts"]), reverse=True)
    return {"query": query, "posts": posts, "result_count": len(posts),
            "warning": "Public posts are unverified leads. Confirm team news with the club, league or a trusted reporter before using it."}
