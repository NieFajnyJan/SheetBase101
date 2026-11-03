from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Office member. Builds on Django's user (first_name, last_name, username, password...),
    so login, admin and later Google/Discord login work out of the box."""

    email = models.EmailField("email address", unique=True)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.get_full_name() or self.username
