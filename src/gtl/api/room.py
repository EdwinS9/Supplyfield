from gtl.board import SqliteBoard
from gtl.contracts import HumanAnswer, Post


class BoardControlRoom:
    def __init__(self, board: SqliteBoard) -> None:
        self.board = board

    def publish(self, post: Post) -> None:
        """The published item is already visible on the Board."""

    def poll(self) -> list[HumanAnswer]:
        return self.board.take_answers()
