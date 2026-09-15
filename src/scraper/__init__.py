"""Módulo de scraping e coleta de materiais/tarefas do Moodle."""

from src.scraper.moodle_scraper import (
    Course,
    Assignment,
    CourseAnnouncement,
    CourseMaterial,
    MoodleScraper,
)
from src.scraper.canvas_quiz import CanvasQuizAutomator
from src.scraper.canvas_coding import CanvasCodingAutomator

__all__ = [
    "Course",
    "Assignment",
    "CourseAnnouncement",
    "CourseMaterial",
    "MoodleScraper",
    "CanvasQuizAutomator",
    "CanvasCodingAutomator",
]


