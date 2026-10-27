"""课件层：把真实 pptx 变成"可渲染、可锚定、可操作"的课件结构。

对外主要入口：
    from deck.store import load_deck, save_deck, list_decks
    from deck.pptx_ingest import ingest_pptx
"""

from deck.schema import (  # noqa: F401
    DeckDoc,
    Element,
    Fill,
    GradientStop,
    LineStyle,
    Paragraph,
    PictureData,
    RunStyle,
    SlideDoc,
    Step,
    TableCell,
    TableData,
    TextBody,
)
