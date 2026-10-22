"""Numéro de version de l'application.

Module séparé pour éviter les imports circulaires : ``__init__.py`` et les
modules de l'interface peuvent l'importer sans dépendre du package complet.
"""

APP_VERSION = "1.5"
