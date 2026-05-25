import os

from temporalio import activity

from classifiers import RuleBasedClassifier, get_classifier
from models import PackageChecks, Verdict


@activity.defn(name="activities.classifier.classify")
async def classify(signals: PackageChecks) -> Verdict:
    clf = get_classifier()
    if not os.environ.get("ANTHROPIC_API_KEY") and isinstance(clf, RuleBasedClassifier):
        activity.logger.info("No ANTHROPIC_API_KEY — using rule-based classifier")
    else:
        activity.logger.info("Using classifier: %s", type(clf).__name__)
    return await clf.classify(signals)
