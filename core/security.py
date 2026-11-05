"""共享的凭据比较。空凭据始终拒绝。"""
import hmac


def secret_equal(left, right):
    return bool(left and right) and hmac.compare_digest(
        str(left).encode("utf-8"), str(right).encode("utf-8"))
