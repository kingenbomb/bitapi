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
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape

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
        conn = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                               context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
        conn.ehlo()
        if sec == "starttls":
            conn.starttls(context=ssl.create_default_context())
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


# ---- 邮件模板(纯文本 + 表格布局与内联样式,兼容 QQ / Gmail) ----


def _mail_html(site_name, title, intro, content, notice):
    brand = escape(str(site_name))
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background-color:#f5f3ef;color:#292824;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif;">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{escape(intro)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f5f3ef;">
<tr><td align="center" style="padding:32px 12px;">
<!--[if mso]><table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background-color:#ffffff;border:1px solid #e9e4dc;border-radius:20px;">
<tr><td style="padding:26px 28px;border-bottom:1px solid #eeeae4;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td style="font-size:22px;line-height:30px;font-weight:750;letter-spacing:-0.5px;color:#292824;">{brand}<span style="color:#a8613c;">.</span></td>
    <td align="right" style="font-size:12px;line-height:20px;color:#928b80;">账号服务</td>
  </tr></table>
</td></tr>
<tr><td style="padding:32px 28px 28px;">
  <p style="margin:0 0 12px;font-size:12px;line-height:20px;font-weight:600;letter-spacing:1px;color:#a8613c;">安全通知</p>
  <h1 style="margin:0 0 14px;font-size:27px;line-height:38px;font-weight:700;letter-spacing:-0.5px;color:#292824;">{escape(title)}</h1>
  <p style="margin:0 0 26px;font-size:14px;line-height:24px;color:#79736a;">{escape(intro)}</p>
  {content}
  <p style="margin:24px 0 0;font-size:13px;line-height:23px;color:#79736a;">{escape(notice)}</p>
</td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #eeeae4;font-size:12px;line-height:21px;color:#a19a90;">如果不是你本人操作，忽略此邮件即可。</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
<p style="margin:20px 0 0;font-size:11px;line-height:20px;color:#a19a90;">{brand} · 账号与安全</p>
</td></tr></table>
</body></html>"""

def reset_password_mail(site_name, link, ttl_minutes):
    subject = f"[{site_name}] 重置密码"
    text = (f"你(或有人)在 {site_name} 请求重置密码。\n\n"
            f"打开下面的链接设置新密码,{ttl_minutes} 分钟内有效,只能用一次:\n{link}\n\n"
            f"如果不是你本人操作,忽略这封邮件即可,密码不会被改动。")
    url = escape(str(link), quote=True)
    content = f"""<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td align="center" bgcolor="#a8613c" style="border-radius:10px;">
<a href="{url}" style="display:inline-block;padding:15px 28px;color:#ffffff;background-color:#a8613c;border:1px solid #a8613c;border-radius:10px;font-size:15px;line-height:22px;font-weight:600;text-decoration:none;">设置新密码 &nbsp;→</a>
</td></tr></table>
<p style="margin:18px 0 0;font-size:12px;line-height:21px;color:#928b80;">链接 {escape(str(ttl_minutes))} 分钟内有效，使用后即失效。</p>
<p style="margin:24px 0 8px;font-size:12px;line-height:20px;color:#928b80;">若按钮无法打开，请复制下方链接到浏览器：</p>
<p style="margin:0;font-size:12px;line-height:21px;word-break:break-all;"><a href="{url}" style="color:#a8613c;text-decoration:underline;word-break:break-all;">{url}</a></p>"""
    html = _mail_html(site_name, "重新设置密码", "我们收到了你的密码重置请求。点击下方按钮，为账号设置一个新密码。",
                      content, "请勿将此链接转发给他人。未完成重置前，你的原密码仍然有效。")
    return subject, text, html


def verify_email_mail(site_name, code, ttl_minutes):
    subject = f"[{site_name}] 邮箱验证码 {code}"
    text = (f"你的 {site_name} 邮箱验证码是:{code}\n\n"
            f"{ttl_minutes} 分钟内有效。如果不是你本人操作,忽略即可。")
    content = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fbf6ef;border:1px solid #efe2d2;border-radius:14px;">
<tr><td align="center" style="padding:22px 12px 10px;font-size:12px;line-height:20px;color:#92816b;">你的验证码</td></tr>
<tr><td align="center" style="padding:0 12px 14px;font-family:'SFMono-Regular',Consolas,'Liberation Mono',monospace;font-size:36px;line-height:46px;font-weight:700;letter-spacing:6px;color:#a8613c;">{escape(str(code))}</td></tr>
<tr><td align="center" style="padding:0 12px 22px;font-size:12px;line-height:20px;color:#92816b;">{escape(str(ttl_minutes))} 分钟内有效 · 仅可使用一次</td></tr>
</table>"""
    html = _mail_html(site_name, "验证你的邮箱", "在页面输入下方验证码，完成邮箱验证。",
                      content, "为保护账号安全，请勿将验证码告知或转发给他人。")
    return subject, text, html


def test_mail(site_name):
    subject = f"[{site_name}] SMTP 测试邮件"
    text = f"这是 {site_name} 管理台发出的测试邮件。收到即说明 SMTP 配置正确。"
    content = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f0f7f2;border:1px solid #dcebdd;border-radius:14px;">
<tr><td style="padding:22px 24px;font-size:15px;line-height:25px;font-weight:600;color:#477052;">邮件发送成功</td></tr>
<tr><td style="padding:0 24px 22px;font-size:13px;line-height:23px;color:#6f8273;">收到这封邮件，说明本站已成功连接邮件服务并完成投递。</td></tr>
</table>"""
    html = _mail_html(site_name, "邮件服务已连接", "这是一封来自管理控制台的测试邮件。",
                      content, "这封邮件仅用于确认发信配置，无需进行任何操作。")
    return subject, text, html
