from abc import ABC, abstractmethod


class CourseDatabaseInterface(ABC):
    """
    Abstract contract for any database backend used in the Canvas-to-HAX pipeline.
    Implementations must satisfy all abstract methods before the pipeline will run.
    """

    @abstractmethod
    def setup_database(self): pass

    @abstractmethod
    def insert_raw_item(
        self, item_id: str, course_id: str, item_type: str,
        title: str, raw_content: str, status: str = "PENDING",
    ): pass

    @abstractmethod
    def get_pending_items(self, course_id: str) -> list: pass

    @abstractmethod
    def get_items_for_evaluation(self, course_id: str) -> list:
        """Return items that have not yet been evaluated (exclude syllabus)."""

    @abstractmethod
    def update_item_evaluation(
        self, item_id: str, course_id: str, evaluation_json: str
    ) -> None:
        """Persist evaluation JSON for a single item."""

    @abstractmethod
    def update_enhanced_item(
        self, item_id: str, course_id: str, ai_markdown: str
    ): pass

    @abstractmethod
    def get_completed_items(self, course_id: str) -> list: pass

    @abstractmethod
    def get_item_by_type(self, course_id: str, item_type: str) -> dict | None: pass
