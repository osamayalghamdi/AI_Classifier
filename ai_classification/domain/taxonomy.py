"""Predefined classification taxonomies for incident tickets.

Hajj and Umrah taxonomy. Generated from the live service catalog.
Service names match actual_service from the API exactly (with system suffix).
Covers Nusuk Masar Haj, Nusuk Masar Umrah, OldSM, and Other.

SERVICES_BY_SYSTEM is a 3-level hierarchy:
  AffectedSystem -> Service -> list[SubService]

A flat view (dict[AffectedSystem, list[str]]) is generated automatically
for LLM prompting and validation via flatten_services(). Sub-services
are exposed as dot-path strings: "Service.SubService".

Pipeline position: 15_models — system/service/offering hierarchy."""

from enum import StrEnum


class AffectedSystem(StrEnum):
    nusuk_masar_haj = "Nusuk Masar Haj"
    nusuk_masar_umrah = "Nusuk Masar Umrah"
    old_sm = "OldSM"
    other = "Other"


class IncidentType(StrEnum):
    spike = "Spike"
    degradation = "Degradation"
    unavailability = "Unavailability"
    outage = "Outage"


class Severity(StrEnum):
    critical = "Critical"
    major = "Major"
    minor = "Minor"
    cosmetic = "Cosmetic"


class Urgency(StrEnum):
    immediate = "Immediate"
    high = "High"
    medium = "Medium"
    low = "Low"


class Category(StrEnum):
    software = "Software"
    performance = "Performance"
    configuration = "Configuration"
    security = "Security"
    network_issue = "Network Issue"
    integration = "Integration"
    data_issue = "Data Issue"
    human_error = "Human Error"
    external = "External / Third Party"
    other = "Other"


# --- Hierarchical service definitions -------------------------------
# System -> Service -> [SubService, ...]
# Service names match actual_service from the live API (with system suffix).

SERVICES_BY_SYSTEM: dict[AffectedSystem, dict[str, list[str]]] = {
    AffectedSystem.nusuk_masar_haj: {
        '7.1 Invoicing and Billing - Nusuk Masar Haj': [
            'Bill Expiry/Cancellation',
            'Bill Generation',
            'Bill Payment',
            'installments payments',
            'Payment Notification',
        ],
        '7.2 Bank Account Management - Nusuk Masar Haj': [
            'Activate electronic wallet',
            'Bank Statement request',
            'Load the electronic wallet via bank transfer',
            'Refund amounts in the electronic wallet to the pilgrim',
        ],
        'Between cities - Nusuk Masar Haj': [
            'Create between cities  Request',
            'Create Final departure  Request',
            'Manage Buses',
            'Naqaba Confirmation Management',
            'Private Vehicles Log',
            'Register Arrival/Departure for in house',
        ],
        'camps - Nusuk Masar Haj': [
            'Add Camp',
            'Change quota request',
            'Deactivate/Activate Camp',
            'Edit Camp details',
        ],
        'contracts - Nusuk Masar Haj': [
            'Added the new house and food services.',
            'Cancel Contract with Accommodation Service Providers',
            'Cancel Contract with Catering Service Providers',
            'Cancel Contract with Transport Companies',
            'Contract with Accommodation Service Providers',
            'Contract with Catering Service Providers',
            'Contract with Portering Service Providers',
            'Contract with Transport Companies',
            'Edit the service details of specific house and food Category city.',
            'View Lifting Provider Details (HPC)',
            'viewed the list of house and food services.',
        ],
        'financial reports - Nusuk Masar Haj': [
            'Final report foe EH',
            'Final report foe LH',
            'التقرير الختامي لمكاتب شؤوون الحج',
        ],
        'Financial Transactions - Nusuk Masar Haj': [
            'view and print contracts',
            'view tansactions list',
            'view transaction type and refrence',
            'view wallet amount',
        ],
        'Funds & Refund Management - Nusuk Masar Haj': [
            'Approve/Reject Refund Money Request',
            'Create Refund Money Request',
            'Fund Distribution',
            'View Refund Money Request Details',
            'View Refund Money Requests list',
        ],
        'Guest Without Baggage - Bags Data Reports': [
            'Baggage Data Details (Arrival)',
            'Baggage Data List (Arrival)',
            'Confirm Baggage Receipt (Arrival)',
        ],
        'Guest Without Baggage Contracts Reports': [
            'Contract Details (Contracts)',
            'Guest Without Bag Contracts List (Contracts)',
        ],
        'hajj B2C local resrevation - Nusuk Masar Haj': [
            'Permits Issuance',
        ],
        'Housing Preference Services': [
        ],
        'inquiry - Nusuk Masar Haj': [
            'inquiry',
        ],
        'Integration - Nusuk Masar Haj': [
            'Makkah Municipality (Holy Capital Municipality)',
        ],
        'Istiqbal - Nusuk Masar Haj': [
            'Add Pilgrims to Passenger Manifest',
            'Assign Guide to Bus',
            'Create Departure Proof',
            'Create Passenger Manifest',
            'Print and Close Passenger Manifest',
            'Register Bus Arrival at Guidance Center',
        ],
        'licensing - Nusuk Masar Haj': [
        ],
        'Mashaeer Boarding Manifest Reports': [
            'Approve Boarding Manifest',
            'Boarding Manifest Details',
            'Boarding Manifest List',
            'Create Boarding Manifest',
            'Create Housing Readiness Group',
            'Create New Template',
            'Delete Boarding Manifest',
            'Edit Boarding Manifest',
            'End Trip',
            'Housing Readiness Group Details',
            'Housing Readiness Group List',
            'Pilgrim Templates List',
            'Start Trip',
            'Trip Details',
            'Trips List',
        ],
        'nusuk hajj services - Nusuk Masar Haj': [
        ],
        'Package Management - Nusuk Masar Haj': [
            'Approve a Customized Package',
            'Cancel the Package',
            'Change Service Items',
            'Create  Package - Step of Additional Services',
            'Create  Package - Step of Prices',
            'Define Camps',
            'Edit Package',
            'Increase Package Capacity',
            'Publish Packages',
            'View Package Details',
            'View Packages',
            'View Packages / Search',
        ],
        'pilgrim groups and issue permit - Nusuk Masar Haj': [
            'Add Pilgrim Data',
            'Attach Required Documents',
            'Create Group',
            'Issue Permits',
            'Issue Visas',
            'Landmarks and Stations of the Hajj Journey (Hajj Planner)',
            'Link Service Items with Group',
            'Process Request for Full Refund for Special Cases',
            'Read Hajj Passport',
            'Select Pilgrims',
            'Send Group to External Parties',
            'Send Group to National Information Center',
            'View Groups list',
            'View Pilgrim Details',
            'View Pilgrims list',
        ],
        'Pilgrim Type - Nusuk Masar Haj': [
            'View Pilgrim Type Details',
            'View Pilgrim Types',
        ],
        'Pre Arrival - Nusuk Masar Haj': [
            'Confirm Pre Arrival Data',
            'Enter Pre Arrival Data',
            'Update Pre Arrival Data',
        ],
        'Public Services - Nusuk Masar Haj': [
            'Forgot My Email',
            'license Request service',
            'Registeration and Activation service',
            'Seasonal activation service',
            'Sign In / sign in with nafath',
            'Sign up',
        ],
        'Registration - Nusuk Masar Haj': [
            'allocate pilgrim Quata ( SPC)',
            'Create Registration Request (FPC)',
            'Create Registration Request (HPC)',
            'Create Registration Request (SPC)',
            'Edit/Resubmit Registration Request (SPC)',
            'Fill out the application form /Complete passport information/ Submit',
            'Register a new user on the platform',
            'Register Guides / Apply Request to become a tour guide',
            'register with nafath',
            'Submits the required attachments',
            'View HPC Registration Request List (MOHU)',
            'View Registration Request Details (SPC)',
            'View Registration Request Details(FPC)',
            'View Registration Request Details(HPC)',
            'View Registration Request List (FPC)',
            'View Registration Request List (SPC)',
            'View Registration Request List(HPC)',
            'Viewing the Application’s Status',
        ],
        'Service Provider License Issuance': [
            'Submit External Hajj Company License Request',
        ],
        'suggestion - Nusuk Masar Haj': [
            'suggestion',
        ],
        'System/Application - Nusuk Masar Haj': [
            'Backend Latency',
            'Error Spikes',
            'Intermittent timeout/failure',
            'Service Unavailability',
            'Slowness',
            'Transaction issue',
        ],
        'Training Course management - Nusuk Masar Haj': [
            'Perform Tests for Guides',
        ],
        'User Management - Nusuk Masar Haj': [
            'Activate/Deactivate Role',
            'Activate/Deactivate User',
            'Add/Remove Permissions',
            'Add/Remove User from Group',
            'Assign User as Entity Administrator',
            'Change My Email Address',
            'Change My Mobile Number',
            'Create a New User',
            'Create Group',
            'Create/Edit Role',
            'Create/Edit/View a Department',
            'Delete User',
            'Edit Group Details',
            'Edit My Information',
            'Edit User Details',
            'Sign In / sign in with nafath',
            'Update profile information restriction',
            'View Departments List & Delete Action',
            'View Group Details',
            'View Groups list',
            'View Role Details',
            'View Roles List',
            'View User Details',
            'View Users List',
        ],
    },
    AffectedSystem.nusuk_masar_umrah: {
        'System/Application - Nusuk Masar Umrah': [
        ],
    },
    AffectedSystem.old_sm: {
        'OldSM': [
            'OldSM',
        ],
    },
    AffectedSystem.other: {
        "General / Unspecified": [],
    },
}


# --- Flat view (backward-compatible) --------------------------------

def flatten_services(
    hierarchy: dict[AffectedSystem, dict[str, list[str]]] | None = None,
) -> dict[AffectedSystem, list[str]]:
    """Produce a flat ``{system: [service, ...]}`` dict from the hierarchy.

    Each service name plus its dot-path sub-services (``"Service.SubService"``)
    are included so the LLM can pick either granularity.
    """
    if hierarchy is None:
        hierarchy = SERVICES_BY_SYSTEM
    flat: dict[AffectedSystem, list[str]] = {}
    for system, services in hierarchy.items():
        entries: list[str] = []
        for svc, subs in services.items():
            entries.append(svc)
            for sub in subs:
                entries.append(f"{svc}.{sub}")
        flat[system] = entries
    return flat
