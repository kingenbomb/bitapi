"""公开注册仅接受 QQ 数字主邮箱和 Gmail 无点号、无加号的主地址。"""
import re


def primary_email(value):
    email = str(value).strip().lower()
    if re.fullmatch(r"[1-9][0-9]*@qq\.com", email):
        return email
    if re.fullmatch(r"[a-z0-9]+@gmail\.com", email):
        return email
    raise ValueError("仅支持数字 QQ 邮箱和 Gmail 主邮箱；Gmail 请去掉点号，不支持 + 别名")
