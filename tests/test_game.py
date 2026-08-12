from app.game import FREE_SPACE, generate_card, has_bingo, label_for, validate_card


def test_generated_cards_are_valid_and_unique() -> None:
    cards = [generate_card() for _ in range(20)]
    assert all(validate_card(card) for card in cards)
    assert len({str(card) for card in cards}) == len(cards)
    assert all(card[2][2] == FREE_SPACE for card in cards)


def test_generated_columns_are_shuffled_instead_of_vertically_sorted() -> None:
    cards = [generate_card(f"shuffle-{index}") for index in range(5)]
    columns = [
        [card[row][column] for row in range(5) if card[row][column] != FREE_SPACE]
        for card in cards
        for column in range(5)
    ]
    assert any(column != sorted(column) for column in columns)


def test_row_column_diagonal_and_four_corners_can_win() -> None:
    card = generate_card()
    assert has_bingo(card, card[0])
    assert has_bingo(card, [card[row][3] for row in range(5)])
    assert has_bingo(card, [card[index][index] for index in range(5)])
    assert has_bingo(card, [card[0][0], card[0][4], card[4][0], card[4][4]])


def test_incomplete_line_does_not_win() -> None:
    card = generate_card()
    assert not has_bingo(card, card[0][:3])
    assert not has_bingo(card, [card[0][0], card[0][4], card[4][0]])


def test_ball_labels() -> None:
    assert label_for(1) == "B-1"
    assert label_for(16) == "I-16"
    assert label_for(75) == "O-75"
