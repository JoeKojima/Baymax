"""Where staff requests go. Shared by the runtime's tools and the dashboard's email sender."""

ROUTES = {
    # category: (care kind, who Ember tells the resident it told, label staff see)
    "maintenance": ("request", "maintenance", "Maintenance"),
    "housekeeping": ("request", "housekeeping", "Housekeeping"),
    "front_desk": ("request", "the front desk", "Front desk"),
    "dining": ("request", "the dining team", "Dining"),
    "care_aide": ("help_request", "the care staff", "Care aide"),
}
