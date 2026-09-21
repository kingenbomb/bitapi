#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发信 —— 标准库 smtplib,零依赖。找回密码、邮箱验证、低余额提醒都走这里。

参数从 core.site_settings 取(管理台存库的值覆盖环境变量),所以站长在网页上
改完 SMTP 立刻生效、不用重启;密码只写不读,管理端接口只回「设了没有」。

同步发送、10 秒超时:找回密码这条路上「发不出去」必须立刻告诉用户
(「邮件服务异常,请联系站长」),而不是让人守着收件箱等一封永远不来的信。
低余额提醒这类旁路通知由调用方自己放到线程里,别拖累请求。
"""
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from core import site_settings as S
from core.hooks import emit

TIMEOUT = 10


class MailError(RuntimeError):
    pass


def configured():
    """host 与发件地址都有才算配了。少一个就是没配 —— 半配的状态下发信必失败,
    不如在入口处说清楚。"""
    return bool(S.smtp_host() and S.smtp_from())


def _connect():
    host, port, sec = S.smtp_host(), int(S.smtp_port()), S.smtp_security()
    if sec == "ssl":
        conn = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
    else:
        conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
        conn.ehlo()
        if sec == "starttls":
            conn.starttls()
            conn.ehlo()
    user, pw = S.smtp_user(), S.smtp_pass()
    if user:
        conn.login(user, pw)
    return conn


def send(to, subject, text, html=None):
    """发一封。失败抛 MailError,错误文本可直接给管理员看(测试发信那个按钮要它)。"""
    if not configured():
        raise MailError("邮件服务未配置(SMTP 主机或发件地址为空)")
    msg = EmailMessage()
    from_name = S.smtp_from_name()
    msg["From"] = formataddr((from_name, S.smtp_from())) if from_name else S.smtp_from()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=S.smtp_from().split("@")[-1] or None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        conn = _connect()
        try:
            conn.send_message(msg)
        finally:
            try:
                conn.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        raise _failed(to, f"SMTP 认证失败:{_smtp_text(e)}", e)
    except smtplib.SMTPRecipientsRefused as e:
        raise _failed(to, f"收件地址被拒:{to}", e)
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        raise _failed(to, f"SMTP 发送失败:{_smtp_text(e)}", e)


def _failed(to, text, cause):
    """发 mail.failed 再抛:SMTP 坏了的时候第一个找回密码的用户会撞上,
    而站长不该等到用户来问。"""
    emit("mail.failed", to=to, error=text)
    err = MailError(text)
    err.__cause__ = cause
    return err


def _smtp_text(e):
    detail = getattr(e, "smtp_error", None)
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    return str(detail or e)


# ---- 邮件模板(纯文本 + 简单 HTML,不引模板引擎) ----

def reset_password_mail(site_name, link, ttl_minutes):
    subject = f"[{site_name}] 重置密码"
    text = (f"你(或有人)在 {site_name} 请求重置密码。\n\n"
            f"打开下面的链接设置新密码,{ttl_minutes} 分钟内有效,只能用一次:\n{link}\n\n"
            f"如果不是你本人操作,忽略这封邮件即可,密码不会被改动。")
    html = (f"<p>你(或有人)在 <b>{site_name}</b> 请求重置密码。</p>"
            f"<p>点击下面的链接设置新密码,<b>{ttl_minutes} 分钟</b>内有效,只能用一次:</p>"
            f'<p><a href="{link}">{link}</a></p>'
            f"<p style=\"color:#888\">如果不是你本人操作,忽略这封邮件即可,密码不会被改动。</p>")
    return subject, text, html


def verify_email_mail(site_name, code, ttl_minutes):
    subject = f"[{site_name}] 邮箱验证码 {code}"
    text = (f"你的 {site_name} 邮箱验证码是:{code}\n\n"
            f"{ttl_minutes} 分钟内有效。如果不是你本人操作,忽略即可。")
    html = (f"<p>你的 <b>{site_name}</b> 邮箱验证码是:</p>"
            f'<p style="font-size:28px;letter-spacing:6px;font-weight:700">{code}</p>'
            f"<p style=\"color:#888\">{ttl_minutes} 分钟内有效。如果不是你本人操作,忽略即可。</p>")
    return subject, text, html


def test_mail(site_name):
    subject = f"[{site_name}] SMTP 测试邮件"
    text = f"这是 {site_name} 管理台发出的测试邮件。收到即说明 SMTP 配置正确。"
    return subject, text, None
