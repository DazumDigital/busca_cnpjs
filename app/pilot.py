"""Limites conservadores da primeira execução; não habilita provedores."""
from dataclasses import replace


def pilot_settings(settings):
    return replace(settings, max_companies=min(settings.max_companies, 10),
                   batch_size=min(settings.batch_size, 10), concurrency=1,
                   retries=min(settings.retries, 1), queries=min(settings.queries, 2),
                   candidates=min(settings.candidates, 3),
                   website_max_requests=min(settings.website_max_requests, 120),
                   website_rpm=min(settings.website_rpm, 6),
                   website_delay=max(settings.website_delay, 5))


def pilot_policies(policies):
    """Tetos locais: nunca aumentam a cota documental ou atestam gratuidade."""
    result = {name: dict(policy) for name, policy in policies.items()}
    for name, cap in (("brasilapi", 20), ("serper", 40)):
        if name not in result:
            continue
        for key, maximum in (("max_requests", cap), ("per_minute", 2), ("per_hour", cap), ("per_day", cap)):
            value = result[name].get(key)
            if type(value) is int:
                result[name][key] = min(value, maximum)
    return result
