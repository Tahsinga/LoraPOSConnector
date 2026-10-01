import json
import logging

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from loraApi.state_store import load_state, save_state

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Restore users and cancelled invoice count from the JSON state file.'

    def handle(self, *args, **options):
        state = load_state()
        User = get_user_model()
        logger.info('Restoring seeded users from state: %s', state.get('users', []))

        for user_data in state.get('users', []):
            username = str(user_data.get('username', '')).strip()
            password = str(user_data.get('password', '')).strip()
            if not username or not password:
                logger.warning('Skipping invalid seeded user entry: %s', user_data)
                continue

            user, created = User.objects.get_or_create(username=username)
            previous_superuser = user.is_superuser
            password_changed = not user.check_password(password)
            role_changed = user.is_staff is not True or user.is_superuser != (username == 'Admin')
            if password_changed:
                user.set_password(password)
            user.is_staff = True
            user.is_superuser = (username == 'Admin')
            if created or password_changed or role_changed:
                user.save()

            logger.info(
                'Seed account sync: username=%s created=%s password_valid=%s is_staff=%s is_superuser=%s previous_superuser=%s',
                username,
                created,
                not password_changed,
                user.is_staff,
                user.is_superuser,
                previous_superuser,
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created user: {username}'))
            else:
                self.stdout.write(self.style.WARNING(f'Updated user: {username}'))

        self.stdout.write(self.style.SUCCESS('State restore complete.'))
