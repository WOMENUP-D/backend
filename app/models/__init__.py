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
from app.models.career_path import CareerPath, UserCareerPath
from app.models.consent import ConsentLog
from app.models.integration import IntegrationEvent
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.learning_path import LearningPath, LearningPathItem, UserLearningPath
from app.models.mentor import MentorProfile, MentorSession
from app.models.news import NewsPost
from app.models.notification import Notification, NotificationPreference
from app.models.opportunity import Application, Opportunity, OutcomeRecord, SavedOpportunity
from app.models.organization import Organization, OrganizationInvitation, OrganizationMember
from app.models.plan import DevelopmentPlan, PlanItem
from app.models.portfolio import PortfolioProject
from app.models.practice import PracticalTask, TaskAttempt
from app.models.profile import Goal, Profile
from app.models.program import (
    Certificate,
    Enrollment,
    Program,
    ProgramLesson,
    ProgramModule,
)
from app.models.skill import Skill, SkillEvidence, UserSkill
from app.models.user import OtpChallenge, User, UserRole

__all__ = [
    "AiInteraction",
    "Application",
    "Assessment",
    "AssessmentAnswer",
    "AssessmentQuestion",
    "AuditLog",
    "Base",
    "CareerPath",
    "Certificate",
    "ConsentLog",
    "DevelopmentPlan",
    "DevelopmentScore",
    "Enrollment",
    "Goal",
    "IntegrationEvent",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "LearningPath",
    "LearningPathItem",
    "MentorProfile",
    "MentorSession",
    "NewsPost",
    "Notification",
    "NotificationPreference",
    "Opportunity",
    "Organization",
    "OrganizationInvitation",
    "OrganizationMember",
    "OtpChallenge",
    "OutcomeRecord",
    "PlanItem",
    "PortfolioProject",
    "PracticalTask",
    "Profile",
    "Program",
    "ProgramLesson",
    "ProgramModule",
    "PageView",
    "RiskFlag",
    "SavedOpportunity",
    "Skill",
    "SkillEvidence",
    "TaskAttempt",
    "User",
    "UserCareerPath",
    "UserLearningPath",
    "UserRole",
    "UserSkill",
]
