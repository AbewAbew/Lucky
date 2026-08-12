from __future__ import annotations

import secrets
from random import Random
from collections.abc import Iterable, Sequence

LETTERS = "BINGO"
FREE_SPACE = 0


def generate_card(seed: int | str | bytes | None = None) -> list[list[int]]:
    """Generate a valid 5x5, 75-ball bingo card with a free centre."""
    random = secrets.SystemRandom() if seed is None else Random(seed)
    columns: list[list[int]] = []
    for column_index in range(5):
        start = column_index * 15 + 1
        columns.append(random.sample(range(start, start + 15), 5))
    columns[2][2] = FREE_SPACE
    return [[columns[column][row] for column in range(5)] for row in range(5)]


def validate_card(card: Sequence[Sequence[int]]) -> bool:
    if len(card) != 5 or any(len(row) != 5 for row in card):
        return False
    values: set[int] = set()
    for row_index, row in enumerate(card):
        for column_index, number in enumerate(row):
            if row_index == 2 and column_index == 2:
                if number != FREE_SPACE:
                    return False
                continue
            low = column_index * 15 + 1
            high = low + 14
            if number < low or number > high or number in values:
                return False
            values.add(number)
    return True


def winning_lines(card: Sequence[Sequence[int]]) -> list[set[int]]:
    lines = [set(row) for row in card]
    lines.extend({card[row][column] for row in range(5)} for column in range(5))
    lines.append({card[index][index] for index in range(5)})
    lines.append({card[index][4 - index] for index in range(5)})
    lines.append({card[0][0], card[0][4], card[4][0], card[4][4]})
    return lines


def has_bingo(card: Sequence[Sequence[int]], eligible_numbers: Iterable[int]) -> bool:
    eligible = set(eligible_numbers) | {FREE_SPACE}
    return any(line <= eligible for line in winning_lines(card))


def label_for(number: int) -> str:
    if not 1 <= number <= 75:
        raise ValueError("Bingo number must be between 1 and 75")
    return f"{LETTERS[(number - 1) // 15]}-{number}"
