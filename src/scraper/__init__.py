"""Módulo de scraping e coleta de materiais/tarefas do Moodle."""

from src.scraper.moodle_scraper import (
    Course,
    Assignment,
    CourseAnnouncement,
    CourseMaterial,
    MoodleScraper,
)

__all__ = [
    "Course",
    "Assignment",
    "CourseAnnouncement",
    "CourseMaterial",
    "MoodleScraper",
]
