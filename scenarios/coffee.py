"""Vision-сценарий чтения кофейной гущи."""
from __future__ import annotations

from llm_client import analyze_image


VISION_PROMPT = (
    "Ты — бережная ИИ-гадалка. На входе фотография. Сначала на первой строке "
    "напиши ровно один маркер: TYPE: coffee, TYPE: natal или TYPE: invalid. "
    "coffee — чашка/блюдце с кофейной гущей; natal — изображение натальной "
    "карты с кругом, домами, планетами или знаками; invalid — всё остальное, "
    "плохой кадр или нечитаемое изображение. После маркера дай ответ на "
    "русском. Для coffee опиши видимые символы осторожно, как интуитивную "
    "интерпретацию, без страшных предсказаний. Для natal объясни, что удалось "
    "увидеть, и начни астрологический разбор; если не хватает даты, времени, "
    "места или читаемости, задай не больше двух уточняющих вопросов. Для "
    "invalid попроси прислать чёткое фото чашки с гущей или скрин натальной "
    "карты. Для TYPE: natal перед основным текстом добавь отдельные строки "
    "CHART_DATE: YYYY-MM-DD или unknown, CHART_TIME: HH:MM или unknown и "
    "CHART_PLACE: название места или unknown. Не угадывай эти значения. "
    "Не притворяйся, что видишь то, чего на фото нет. Не упоминай "
    "код, Git или технические детали."
)


async def read_photo(
    image_bytes: bytes,
    caption: str = "",
    mime_type: str = "image/jpeg",
) -> str:
    """Classify a photo and return a coffee/natal/invalid vision result."""
    return await analyze_image(image_bytes, caption, VISION_PROMPT, mime_type)


def classify_result(raw_result: str) -> tuple[str, str]:
    """Extract the strict scenario marker returned by the vision model."""
    lines = raw_result.strip().splitlines()
    marker = lines[0].strip().lower() if lines else ""
    if "type: coffee" in marker:
        kind = "coffee"
    elif "type: natal" in marker:
        kind = "natal"
    else:
        kind = "invalid"
    reply_lines = lines[1:]
    if kind == "natal":
        reply_lines = [
            line for line in reply_lines
            if not line.lower().startswith(("chart_date:", "chart_time:", "chart_place:"))
        ]
    reply = "\n".join(reply_lines).strip() or raw_result.strip()
    return kind, reply


def extract_chart_data(raw_result: str) -> dict[str, str]:
    data = {"date": "unknown", "time": "unknown", "place": "unknown"}
    prefixes = {
        "CHART_DATE:": "date",
        "CHART_TIME:": "time",
        "CHART_PLACE:": "place",
    }
    for line in raw_result.splitlines():
        for prefix, key in prefixes.items():
            if line.strip().upper().startswith(prefix):
                value = line.split(":", 1)[1].strip()
                data[key] = value or "unknown"
    return data
