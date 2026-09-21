#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""白嫖社区 community-connect 的服务端 OAuth 客户端。"""
import hashlib
import secrets
import urllib.parse

import httpx

import config

FLOW_TTL = 300
FLOW_COOKIE = "bitapi_community_flow"


class CommunityAuthError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def new_secret():
    return secrets.token_urlsafe(32)


def cookie_secure():
    return urllib.parse.urlsplit(config.COMMUNITY_REDIRECT_URI).scheme == "https"


def _configured():
    if not (config.COMMUNITY_CLIENT_ID and config.COMMUNITY_CLIENT_SECRET and
            config.COMMUNITY_REDIRECT_URI):
        raise CommunityAuthError("community login is not configured")


def _url(path):
    return config.COMMUNITY_BASE_URL.rstrip("/") + path


def _api_url(path):
    """插件的 REST 命名空间由 COMMUNITY_API_PREFIX 定(各站可以改过名)。"""
    return _url("/api/" + config.COMMUNITY_API_PREFIX.strip("/") + path)


def authorize_url(state):
    _configured()
    query = urllib.parse.urlencode({
        "client_id": config.COMMUNITY_CLIENT_ID,
        "redirect_uri": config.COMMUNITY_REDIRECT_URI,
        "state": state,
    })
    return _url("/community-connect") + "?" + query


def exchange_code(code):
    """授权码只在服务端换短 access token；错误正文不向浏览器透传。"""
    _configured()
    try:
        response = httpx.post(
            _api_url("/token"),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.COMMUNITY_CLIENT_ID,
                "client_secret": config.COMMUNITY_CLIENT_SECRET,
                "redirect_uri": config.COMMUNITY_REDIRECT_URI,
            },
            timeout=config.COMMUNITY_HTTP_TIMEOUT,
            follow_redirects=False)
        if response.status_code != 200:
            raise CommunityAuthError("community authorization code was rejected")
        payload = response.json()
    except CommunityAuthError:
        raise
    except (httpx.HTTPError, ValueError) as e:
        raise CommunityAuthError("community token exchange failed") from e
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise CommunityAuthError("community token response is invalid")
    return token


def get_userinfo(access_token):
    """返回已验证且可用的社区资料；稳定身份键只取 userinfo.id。"""
    try:
        response = httpx.get(
            _api_url("/userinfo"),
            headers={"Authorization": "Bearer " + access_token},
            timeout=config.COMMUNITY_HTTP_TIMEOUT,
            follow_redirects=False)
        if response.status_code != 200:
            raise CommunityAuthError("community account is unavailable")
        payload = response.json()
    except CommunityAuthError:
        raise
    except (httpx.HTTPError, ValueError) as e:
        raise CommunityAuthError("community userinfo request failed") from e
    if not isinstance(payload, dict):
        raise CommunityAuthError("community userinfo response is invalid")
    subject = str(payload.get("id") or "").strip()
    username = str(payload.get("username") or "").strip()
    name = str(payload.get("name") or username).strip()
    email = str(payload.get("email") or "").strip().lower()
    if (not subject.isdigit() or int(subject) <= 0 or not username or
            not email or "@" not in email or
            payload.get("email_verified") is not True or
            payload.get("active") is not True):
        raise CommunityAuthError("community userinfo response is invalid")
    return {"id": subject, "username": username, "name": name,
            "email": email}
