"""Explicit Web image estimates; these are never reported as upstream usage.

Output formula follows chatgpt2api utils/image_tokens.py at commit
92dd80e91543157d36a4ddad60e765c5293760e9 (MIT, see LICENSE).
The Web route uses auto quality, whose reference estimate is medium quality.
"""
from io import BytesIO
from PIL import Image

from core.metering import count_tokens


def estimate_image_usage(data, prompt):
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
    patches = ((width + 31) // 32) * ((height + 31) // 32)
    output_tokens = (patches * 33 + 31) // 32
    input_tokens = count_tokens(prompt)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "token_source": "estimate",
            "input_tokens_details": {"text_tokens": input_tokens, "image_tokens": 0, "cached_tokens": 0},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": output_tokens},
            "image_token_estimate": {"method": "chatgpt2api-patch32-auto-v1", "quality": "auto",
                                     "width": width, "height": height}}
