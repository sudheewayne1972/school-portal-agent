"""MCB Digest package — modular pipeline that fetches CHIREC announcements,
extracts timeline-fixed events, and emails a daily digest at 7 PM IST."""
__all__ = ["config", "scraper", "parser", "extractor", "reminders", "digest", "emailer", "main"]
