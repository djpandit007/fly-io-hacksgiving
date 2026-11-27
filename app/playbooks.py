"""Legal playbooks: the five kinds of document Fine Print checks, and what it checks each for.

One source of truth for every domain's red-flag battery (the Jev questions), the fixed plain-English
explanation behind each 💡 button, the curated statutory ruleset per jurisdiction, and the keyword
rules that pick candidate clauses for the ephemeral cross-contract conflict check. Nothing here calls
a provider: Jev answers the questions (app/judge.py) and app/agent.py orchestrates.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field

Domain = Literal["consumer_tos", "privacy_and_ai", "residential_lease", "employment_or_nda", "freelance_or_saas"]
DOMAINS: tuple[Domain, ...] = get_args(Domain)
DEFAULT_DOMAIN: Domain = "consumer_tos"

# Contracts a member signs personally (not a public service's terms): these get the cross-contract
# conflict check (employment/freelance only) and, later, the redline kit.
PERSONAL_DOMAINS: frozenset[Domain] = frozenset({"residential_lease", "employment_or_nda", "freelance_or_saas"})
CONFLICT_DOMAINS: frozenset[Domain] = frozenset({"employment_or_nda", "freelance_or_saas"})


class Playbook(NamedTuple):
    label: str        # what the verdict card shows as the detected kind of document
    describes: str    # the router's criterion for this domain (Jev `choice`)
    flags: dict[str, str]  # flag key -> Jev `noul` question


PLAYBOOKS: dict[Domain, Playbook] = {
    # The original six flags, word for word: existing cards, buttons and metrics keep their meaning.
    "consumer_tos": Playbook(
        "Consumer terms",
        "Terms of service, terms of use, a user agreement or subscription terms for a website, app or consumer service.",
        {
            "auto_renews": "Does the document say a subscription, membership or trial renews or converts to a paid plan automatically?",
            "arbitration": "Does the document require disputes to go to binding arbitration or waive class actions?",
            "data_sharing": "Does the document allow the company to share or sell the user's personal data to third parties for their own purposes, including advertising or AI training?",
            "unilateral_changes": "Does the document let the company change the terms or prices without the user's explicit agreement?",
            "content_licence": "Does the document give the company a broad licence to use, modify or sublicense content the user uploads?",
            "hard_to_cancel": "Does the document make cancelling hard, for example by requiring notice well in advance, a phone call, or a written letter?",
        },
    ),
    "privacy_and_ai": Playbook(
        "Privacy & AI policy",
        "A privacy policy or notice, cookie policy, data processing terms, or AI / machine-learning terms describing how personal data or user content is used.",
        {
            "ai_model_training": "Does the document allow the company to use the user's personal data or content to train, improve or fine-tune AI or machine-learning models?",
            "data_broker_sale": "Does the document allow the company to sell, rent or trade the user's personal data to data brokers or other third parties?",
            "biometric_or_location": "Does the document allow collecting the user's biometric data (face, voice, fingerprints) or precise location?",
            "indefinite_retention": "Does the document let the company keep personal data indefinitely, with no stated time limit, or after the account is closed?",
            "cross_context_ads": "Does the document allow tracking the user across other websites, apps or devices to target advertising?",
            "no_deletion_right": "Does the document refuse, restrict or leave out any way for the user to have their personal data deleted?",
        },
    ),
    "residential_lease": Playbook(
        "Residential lease",
        "A residential lease, rental or tenancy agreement for a home, apartment or room.",
        {
            "auto_lease_renewal": "Does the lease renew or extend automatically for a new term unless the tenant gives notice by a deadline?",
            "repair_cost_shifting": "Does the lease make the tenant pay for repairs or maintenance normally owed by the landlord, such as appliances, plumbing, heating or structural repairs?",
            "early_termination_penalty": "Does the lease charge the tenant a penalty, a fee or all remaining rent for ending the lease early?",
            "non_refundable_fees": "Does the lease charge non-refundable fees, such as cleaning, administration, pet or move-in fees?",
            "security_deposit_trap": "Does the lease let the landlord keep the security deposit for normal wear and tear, automatically, without an itemised list, or return it late?",
            "landlord_unannounced_entry": "Does the lease let the landlord or their agents enter the home for non-emergencies without advance notice to the tenant, or with very short notice?",
            "habitability_waiver": "Does the lease make the tenant accept the home 'as is' or waive the landlord's duty to keep it safe and fit to live in?",
        },
    ),
    "employment_or_nda": Playbook(
        "Employment / NDA",
        "An employment contract, offer letter, invention-assignment agreement (PIIA), non-compete, or non-disclosure or confidentiality agreement between a person and an employer or company.",
        {
            "side_project_ip_capture": "Does the document give the employer inventions, code or creative work the person makes on their own time, with their own equipment, or unrelated to the job?",
            "non_compete_clause": "Does the document stop the person from working for a competitor or starting a competing business after they leave?",
            "broad_non_solicit": "Does the document broadly stop the person from hiring or working with the company's employees, clients or customers after they leave?",
            "equity_or_bonus_clawback": "Does the document let the company take back, cancel or demand repayment of bonuses, equity, stock options or relocation money?",
            "perpetual_confidentiality": "Does the document impose confidentiality duties that never end, or that cover information that is already public?",
            "mandatory_arbitration": "Does the document require employment disputes to go to binding arbitration or waive class or collective actions?",
        },
    ),
    "freelance_or_saas": Playbook(
        "Freelance / SaaS contract",
        "A freelance, contractor, consulting or statement-of-work agreement, or a business software (SaaS) or master services agreement between businesses or with an independent contractor.",
        {
            "uncapped_indemnity": "Does the document make the contractor or customer indemnify the other party with no cap on the amount?",
            "asymmetric_liability_cap": "Does the document cap one party's liability far lower than the other's, or limit only one side's liability?",
            "extended_payment_terms": "Does the document allow payment more than 30 days after invoice, or make payment depend on the client's approval or their own customer paying?",
            "pre_payment_ip_transfer": "Does the document transfer ownership of the work or its intellectual property to the client before the contractor is paid in full?",
            "no_kill_fee_termination": "Does the document let the client end the contract at any time without paying for work in progress or a cancellation (kill) fee?",
            "auto_renewal_lockin": "Does the document renew automatically for a new term, or lock the customer in with long notice periods or early-termination fees?",
        },
    ),
}

# Every flag across every domain, for checks that only need to know a key exists (e.g. 💡 button data).
ALL_FLAGS: dict[str, str] = {key: q for pb in PLAYBOOKS.values() for key, q in pb.flags.items()}

# What each red flag means, in plain English, for the 💡 buttons on verdict cards. Fixed text: showing
# one needs no provider call and no copy of the checked document. Each fits a 200-character pop-up.
FLAG_EXPLANATIONS: dict[str, str] = {
    # consumer_tos (unchanged)
    "auto_renews": "Auto-renewal: the subscription or trial keeps going, and keeps charging you, unless you cancel "
                   "in time. Note the renewal date and how to cancel.",
    "arbitration": "Arbitration: disputes go to a private arbitrator instead of a court, and you usually can't join "
                   "a class action. Some terms let you opt out within a set time.",
    "data_sharing": "Data sharing: the company may pass your personal data to other companies for their own use, "
                    "such as advertising or training AI models.",
    "unilateral_changes": "One-sided changes: the company can change the terms or prices without asking you; "
                          "carrying on using the service counts as agreeing.",
    "content_licence": "Content licence: you let the company use, change and pass on what you upload, often for "
                       "free, worldwide and for as long as it likes.",
    "hard_to_cancel": "Hard to cancel: stopping takes extra steps, like notice well in advance, a phone call or a "
                      "letter, so you may keep paying longer than you want.",
    # privacy_and_ai
    "ai_model_training": "AI training: what you type, upload or say may be used to train the company's AI models. "
                         "Look for a setting or form to opt out.",
    "data_broker_sale": "Data sale: your personal data may be sold or traded to data brokers, who build profiles "
                        "on you and resell them to others.",
    "biometric_or_location": "Sensitive data: the company may collect your face, voice, fingerprints or precise "
                             "location, which can't be changed like a password if leaked.",
    "indefinite_retention": "Kept forever: there is no clear limit on how long your data is stored, even after you "
                            "close your account.",
    "cross_context_ads": "Cross-site tracking: your activity on other sites, apps or devices may be followed to "
                         "target ads at you.",
    "no_deletion_right": "No deletion: the policy gives you no clear way to have your data erased, or limits it "
                         "heavily.",
    # residential_lease
    "auto_lease_renewal": "Auto-renewing lease: the lease rolls into a new term unless you give notice by a "
                          "deadline. Diary the notice date now.",
    "repair_cost_shifting": "Repair costs on you: you may have to pay for repairs the landlord would normally "
                            "cover, like appliances, plumbing or heating.",
    "early_termination_penalty": "Early exit penalty: leaving before the end could cost a fee or all the rent "
                                 "left on the lease.",
    "non_refundable_fees": "Non-refundable fees: money such as cleaning, admin or pet fees that you won't get back, "
                           "whatever happens.",
    "security_deposit_trap": "Deposit trap: the landlord may keep part or all of your deposit for normal wear and "
                             "tear, without itemising, or pay it back late.",
    "landlord_unannounced_entry": "Entry without notice: the landlord may come into your home with little or no "
                                  "warning, not only in emergencies.",
    "habitability_waiver": "'As is' clause: you may be giving up the landlord's duty to keep your home safe, "
                           "heated and fit to live in.",
    # employment_or_nda
    "side_project_ip_capture": "Side projects captured: things you make in your own time, on your own kit, may "
                               "belong to the employer. List prior work and ask for a carve-out.",
    "non_compete_clause": "Non-compete: after you leave you may be barred from working for rivals or starting a "
                          "competing business, for a time or in an area.",
    "broad_non_solicit": "Non-solicit: after you leave you may not be able to hire ex-colleagues or work with the "
                         "company's clients.",
    "equity_or_bonus_clawback": "Clawback: the company can take back bonuses, equity or relocation money, for "
                                "example if you leave early.",
    "perpetual_confidentiality": "Endless secrecy: your duty to keep things confidential never ends, or covers "
                                 "information that is already public.",
    "mandatory_arbitration": "Workplace arbitration: disputes with your employer go to a private arbitrator, not "
                             "a court, and group claims may be waived.",
    # freelance_or_saas
    "uncapped_indemnity": "Uncapped indemnity: you could owe the other side for their losses or legal costs with "
                          "no upper limit.",
    "asymmetric_liability_cap": "Lopsided liability: one side's liability is capped far lower than the other's, "
                                "usually not yours.",
    "extended_payment_terms": "Slow payment: you may wait more than 30 days to be paid, or only once the client "
                              "approves the work or is paid itself.",
    "pre_payment_ip_transfer": "Rights before payment: the client owns your work before paying you in full, so you "
                               "lose leverage if they don't pay.",
    "no_kill_fee_termination": "No kill fee: the client can cancel at any time without paying for work already "
                               "in progress.",
    "auto_renewal_lockin": "Lock-in: the contract renews automatically or is costly to leave, with long notice or "
                           "exit fees.",
}

# The category (judge.CATEGORIES) a found flag's clause is benchmarked in, where one fits.
FLAG_CATEGORY: dict[str, str] = {
    "auto_renews": "renewal", "arbitration": "arbitration", "data_sharing": "data",
    "unilateral_changes": "termination", "content_licence": "content", "hard_to_cancel": "renewal",
    "ai_model_training": "data", "data_broker_sale": "data", "cross_context_ads": "data",
    "indefinite_retention": "data", "no_deletion_right": "data", "biometric_or_location": "data",
    "mandatory_arbitration": "arbitration", "auto_renewal_lockin": "renewal",
    "uncapped_indemnity": "liability", "asymmetric_liability_cap": "liability",
    "early_termination_penalty": "fees", "non_refundable_fees": "fees",
}


def battery(domain: str) -> dict[str, str]:
    """The red-flag questions for a domain; an unknown domain gets the consumer terms battery."""
    return PLAYBOOKS.get(domain, PLAYBOOKS[DEFAULT_DOMAIN]).flags


def label(domain: str) -> str:
    return PLAYBOOKS.get(domain, PLAYBOOKS[DEFAULT_DOMAIN]).label


def router_criteria() -> dict[str, str]:
    return {d: pb.describes for d, pb in PLAYBOOKS.items()}


# --- What a check found, for whoever renders or acts on it ------------------------------------------

class ContractCheck(BaseModel):
    """The detected domain and the flags found on a verdict card (no document text).

    Carried on the card's Outgoing so a later step (e.g. a redline kit button) can decide what to offer.
    """

    model_config = ConfigDict(frozen=True)

    domain: Domain = DEFAULT_DOMAIN
    found: list[str] = Field(default_factory=list)

    @property
    def personal(self) -> bool:
        return self.domain in PERSONAL_DOMAINS

    @property
    def offers_redline(self) -> bool:
        return self.personal and bool(self.found)


# --- Jurisdictions and the statutory ruleset -------------------------------------------------------

JURISDICTIONS: dict[str, str] = {
    "US-CA": "California, USA",
    "US-NY": "New York, USA",
    "US-OTHER": "elsewhere in the USA",
    "EU": "the European Union",
    "UK": "the United Kingdom",
}
CLEAR_WORDS = frozenset({"clear", "none", "off", "unset", "remove"})


class StatutoryRule(NamedTuple):
    law: str       # the statute, as shown to the member
    note: str      # what it may mean, phrased "may be restricted or void under…"
    question: str  # the narrow Jev check that the quoted clause really triggers it


# (jurisdiction, domain, flag) -> rule. Curated and deliberately small: only well-established rules.
STATUTES: dict[tuple[str, str, str], StatutoryRule] = {
    ("US-CA", "employment_or_nda", "non_compete_clause"): StatutoryRule(
        "Cal. Bus. & Prof. Code § 16600",
        "may be void under California law, which bars most employee non-competes",
        "Does `clause` restrain the person from working in a lawful profession, trade or business after the job ends?",
    ),
    ("US-CA", "employment_or_nda", "side_project_ip_capture"): StatutoryRule(
        "Cal. Labor Code § 2870",
        "may be unenforceable under California law for inventions made on your own time and equipment, unrelated to the employer's business",
        "Does `clause` assign to the employer inventions made entirely on the employee's own time without the employer's equipment or information?",
    ),
    ("US-CA", "consumer_tos", "auto_renews"): StatutoryRule(
        "California Automatic Renewal Law (Bus. & Prof. Code § 17600 et seq.)",
        "may be restricted under California law, which requires clear disclosure, consent and an easy online cancellation",
        "Does `clause` describe a consumer subscription or trial that renews or converts to paid automatically?",
    ),
    ("US-CA", "residential_lease", "landlord_unannounced_entry"): StatutoryRule(
        "Cal. Civil Code § 1954",
        "may be restricted under California law, which generally requires 24 hours' written notice before non-emergency entry",
        "Does `clause` let the landlord enter for non-emergencies with less than 24 hours' written notice?",
    ),
    ("US-CA", "residential_lease", "security_deposit_trap"): StatutoryRule(
        "Cal. Civil Code § 1950.5",
        "may be restricted under California law, which limits deductions to damage beyond normal wear and requires an itemised statement within 21 days",
        "Does `clause` let the landlord keep deposit money for normal wear and tear, without itemising, or after 21 days?",
    ),
    ("US-CA", "residential_lease", "habitability_waiver"): StatutoryRule(
        "Cal. Civil Code § 1942.1",
        "may be void under California law, which does not let tenants waive the landlord's repair duties",
        "Does `clause` waive or modify the landlord's duty to keep the home habitable or the tenant's repair rights?",
    ),
    ("US-NY", "consumer_tos", "auto_renews"): StatutoryRule(
        "N.Y. Gen. Bus. Law § 527-a",
        "may be restricted under New York law, which requires clear disclosure, consent and an easy way to cancel automatic renewals",
        "Does `clause` describe a consumer subscription or trial that renews or converts to paid automatically?",
    ),
    ("US-NY", "employment_or_nda", "side_project_ip_capture"): StatutoryRule(
        "N.Y. Labor Law § 203-f",
        "may be unenforceable under New York law for inventions made on your own time and equipment, unrelated to the employer's business",
        "Does `clause` assign to the employer inventions made entirely on the employee's own time without the employer's equipment or information?",
    ),
    ("US-NY", "residential_lease", "security_deposit_trap"): StatutoryRule(
        "N.Y. Gen. Oblig. Law § 7-108",
        "may be restricted under New York law, which caps deposits at one month's rent and requires an itemised return within 14 days",
        "Does `clause` let the landlord keep deposit money for normal wear and tear, without itemising, or after 14 days, or take more than one month's rent?",
    ),
    ("US-NY", "residential_lease", "non_refundable_fees"): StatutoryRule(
        "N.Y. Real Prop. Law § 238-a",
        "may be restricted under New York law, which bans most fees beyond rent and deposit, apart from limited background-check and late fees",
        "Does `clause` charge the tenant a fee other than rent, a security deposit, a background-check fee or a late fee?",
    ),
    ("EU", "consumer_tos", "arbitration"): StatutoryRule(
        "EU Unfair Contract Terms Directive 93/13/EEC",
        "may be unfair and not binding on consumers under EU law, which lists terms excluding court action among presumptively unfair terms",
        "Does `clause` require a consumer to take disputes exclusively to arbitration or otherwise exclude court action?",
    ),
    ("EU", "consumer_tos", "unilateral_changes"): StatutoryRule(
        "EU Unfair Contract Terms Directive 93/13/EEC",
        "may be unfair and not binding on consumers under EU law when the company can change terms without a valid stated reason",
        "Does `clause` let the company change the terms or price unilaterally without a valid reason specified in the contract?",
    ),
    ("EU", "privacy_and_ai", "ai_model_training"): StatutoryRule(
        "GDPR Art. 6 and Art. 21",
        "may be restricted under EU data protection law: AI training needs a lawful basis, and you can object to processing based on legitimate interests",
        "Does `clause` use personal data to train or improve AI models?",
    ),
    ("EU", "privacy_and_ai", "no_deletion_right"): StatutoryRule(
        "GDPR Art. 17",
        "may be restricted under EU data protection law, which gives you a right to have personal data erased in many cases",
        "Does `clause` deny or restrict the user's ability to have their personal data deleted?",
    ),
    ("EU", "privacy_and_ai", "indefinite_retention"): StatutoryRule(
        "GDPR Art. 5(1)(e)",
        "may be restricted under EU data protection law, which requires personal data to be kept no longer than necessary",
        "Does `clause` let the company keep personal data with no time limit or longer than needed for its purpose?",
    ),
    ("UK", "consumer_tos", "arbitration"): StatutoryRule(
        "Arbitration Act 1996 s. 91 and Consumer Rights Act 2015",
        "may be unfair and not binding on consumers under UK law, at least for claims of £5,000 or less",
        "Does `clause` require a consumer to take disputes to arbitration?",
    ),
    ("UK", "consumer_tos", "unilateral_changes"): StatutoryRule(
        "Consumer Rights Act 2015, Sch. 2",
        "may be unfair and not binding on consumers under UK law when the company can change terms without a valid stated reason",
        "Does `clause` let the company change the terms or price unilaterally without a valid reason specified in the contract?",
    ),
    ("UK", "privacy_and_ai", "ai_model_training"): StatutoryRule(
        "UK GDPR Art. 6 and Art. 21",
        "may be restricted under UK data protection law: AI training needs a lawful basis, and you can object to processing based on legitimate interests",
        "Does `clause` use personal data to train or improve AI models?",
    ),
}


def statutes_for(jurisdiction: str | None, domain: str, found: list[str]) -> dict[str, StatutoryRule]:
    """The rules (by flag) that a member's jurisdiction maps to the flags found on this document."""
    if not jurisdiction:
        return {}
    return {f: rule for f in found if (rule := STATUTES.get((jurisdiction, domain, f)))}


# --- Cross-contract conflicts: picking candidate clauses by keyword -------------------------------

MAX_CONFLICT_CANDIDATES = 8
CONFLICT_THRESHOLD = 0.75
CONTRACT_PART_CHARS = 4_000
SERVICE_CLAUSE_CHARS = 3_000

# What a service clause must touch to be compared: AI training, licences over content, or data use.
SERVICE_TOPICS: dict[str, re.Pattern[str]] = {
    "ai": re.compile(r"\b(?:ai|artificial intelligence|machine learning|train(?:ing|s|ed)?|models?)\b", re.IGNORECASE),
    "licence": re.compile(r"licen[cs]e|sublicen|royalty[- ]free|your content|user content|customer content|inputs?\b", re.IGNORECASE),
    "data": re.compile(r"personal data|personal information|\bdata\b|disclos|third part", re.IGNORECASE),
}
# What a contract part must touch: confidentiality, ownership of work, or AI / third-party tools.
CONTRACT_TOPICS: dict[str, re.Pattern[str]] = {
    "confidential": re.compile(r"confidential|non-disclosure|proprietary|trade secret", re.IGNORECASE),
    "ip": re.compile(r"intellectual property|invention|copyright|work product|work made for hire|assign", re.IGNORECASE),
    "ai": re.compile(r"\b(?:ai|artificial intelligence|machine learning|third[- ]party (?:tools?|services?))\b", re.IGNORECASE),
}

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


class Part(NamedTuple):
    heading: str
    body: str


def parts(text: str, size: int = CONTRACT_PART_CHARS) -> list[Part]:
    """A document split at Markdown headings (any level), or into paragraph blocks when it has none."""
    found: list[Part] = []
    heading, body = "", []
    for line in text.splitlines():
        if m := _HEADING.match(line):
            if heading or "".join(body).strip():
                found.append(Part(heading, "\n".join(body).strip()))
            heading, body = m.group(1).strip(), []
        else:
            body.append(line)
    if heading or "".join(body).strip():
        found.append(Part(heading, "\n".join(body).strip()))
    if len(found) > 1:
        return [p for p in found if p.body]
    # No headings: paragraph blocks of up to `size` characters, labelled by their opening words.
    blocks: list[Part] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) > size:
            blocks.append(Part("", current.strip()))
            current = ""
        current += para + "\n\n"
    if current.strip():
        blocks.append(Part("", current.strip()))
    return blocks


def topics(text: str, rules: dict[str, re.Pattern[str]]) -> dict[str, int]:
    return {name: n for name, rx in rules.items() if (n := len(rx.findall(text)))}


def part_label(part: Part, chars: int = 60) -> str:
    if part.heading:
        return part.heading[:chars]
    words = " ".join(part.body.split())
    return (words[: chars - 1].rstrip() + "…") if len(words) > chars else words
