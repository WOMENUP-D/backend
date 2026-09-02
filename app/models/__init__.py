"""SQLAlchemy models. Importing this package registers every table on Base.metadata."""

from app.models.analytics import PageView
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    DevelopmentScore,
)
from app.models.audit import AiInteraction, AuditLog, RiskFlag
from app.models.base import Base
from app.models.consent import ConsentLog
from app.models.integration import IntegrationEvent
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.mentor import MentorProfile, MentorSession
from app.models.news import NewsPost
from app.models.notification import Notification, NotificationPreference
from app.models.opportunity import Application, Opportunity, OutcomeRecord
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.profile import Goal, Profile
from app.models.program import (
    Certificate,
    Enrollment,
    Program,
    ProgramModule,
)
from app.models.user import OtpChallenge, User, UserRole

__all__ = [
    "AiInteraction",
    "Application",
    "Assessment",
    "AssessmentAnswer",
    "AssessmentQuestion",
    "AuditLog",
    "Base",
    "Certificate",
    "ConsentLog",
    "DevelopmentPlan",
    "DevelopmentScore",
    "Enrollment",
    "Goal",
    "IntegrationEvent",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "MentorProfile",
    "MentorSession",
    "NewsPost",
    "Notification",
    "NotificationPreference",
    "Opportunity",
    "OtpChallenge",
    "OutcomeRecord",
    "PlanItem",
    "Profile",
    "Program",
    "ProgramModule",
    "PageView",
    "RiskFlag",
    "User",
    "UserRole",
]
