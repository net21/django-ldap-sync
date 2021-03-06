import logging

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group
from django.db import DataError
from django.db import IntegrityError
from django.utils.module_loading import import_string

from ldap_sync.search import LDAPSearch
from ldap_sync.settings import SyncSettings


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    can_import_settings = True
    help = 'Synchronize users and groups from an authoritative LDAP server'

    def __init__(self, *args, **kwargs):
        super(Command, self).__init__(*args, **kwargs)
        self.settings = SyncSettings()
        self.ldap = LDAPSearch(self.settings)

    def handle(self, *args, **options):
        if self.settings.GROUP_FILTER:
            ldap_groups = self.ldap.search(self.settings.GROUP_FILTER, self.settings.GROUP_ATTRIBUTES.keys())
            self.sync_ldap_groups(ldap_groups)

        if self.settings.USER_FILTER:
            user_attributes = self.settings.USER_ATTRIBUTES.keys() + self.settings.USER_EXTRA_ATTRIBUTES
            ldap_users = self.ldap.search(self.settings.USER_FILTER, user_attributes)
            self.sync_ldap_users(ldap_users)

        self.ldap.unbind()

    def sync_ldap_groups(self, ldap_groups):
        """Synchronize LDAP groups with local group model."""
        for cname, ldap_attributes in ldap_groups:
            defaults = {}

            if not isinstance(ldap_attributes, dict):
                # In some cases attrs is not a dict; skip these invalid groups
                continue

            for ldap_name, field in self.settings.GROUP_ATTRIBUTES.items():
                try:
                    defaults[field] = self.settings.GROUP_ATTRIBUTES[ldap_name][0].decode('utf-8')
                except KeyError:
                    defaults[field] = ''

            groupname = defaults[self.settings.GROUPNAME_FIELD]
            kwargs = {
                self.settings.GROUPNAME_FIELD + '__iexact': groupname,
                'defaults': defaults,
            }

            try:
                group, created = Group.objects.get_or_create(**kwargs)
            except (IntegrityError, DataError) as e:
                logger.error("Error creating group %s: %s" % (groupname, e))
            else:
                if created:
                    logger.debug("Created group %s" % groupname)

        logger.info("Groups are synchronized")

    def sync_ldap_users(self, ldap_users):
        """Synchronize users with local user model."""
        ldap_usernames = set()

        for cname, ldap_attributes in ldap_users:
            defaults = {}

            if not isinstance(ldap_attributes, dict):
                # In some cases attributes is not a dict; skip these invalid users
                continue

            for ldap_name, field in self.settings.USER_ATTRIBUTES.items():
                try:
                    defaults[field] = ldap_attributes[ldap_name][0].decode('utf-8')
                except KeyError:
                    defaults[field] = ''

            username = defaults[self.settings.USERNAME_FIELD].lower()
            kwargs = {
                self.settings.USERNAME_FIELD + '__iexact': username,
                'defaults': defaults,
            }

            try:
                user, created = self.settings.model.objects.get_or_create(**kwargs)
            except (IntegrityError, DataError) as e:
                logger.error("Error creating user %s: %s" % (username, e))
            else:
                updated = False
                if created:
                    logger.debug("Created user %s" % username)
                    user.set_unusable_password()
                else:
                    for name, attr in defaults.items():
                        current_attr = getattr(user, name, None)
                        if current_attr != attr:
                            setattr(user, name, attr)
                            updated = True
                    if updated:
                        logger.debug("Updated user %s" % username)

                for path in self.settings.USER_CALLBACKS:
                    callback = import_string(path)
                    callback(user, ldap_attributes, created, updated)

                # It's possible for an IntegrityError to occur here
                # due to user modifications made by callbacks
                try:
                    user.save()
                except IntegrityError as e:
                    logger.error("Error updating user %s: %s" % (username, e))

                if self.settings.REMOVED_USER_CALLBACKS:
                    ldap_usernames.add(username)

        if self.settings.REMOVED_USER_CALLBACKS:
            django_usernames = set(self.settings.model.objects.values_list(self.settings.USERNAME_FIELD, flat=True))
            for username in django_usernames - ldap_usernames:
                user = self.settings.model.objects.get(**{self.settings.USERNAME_FIELD: username})
                for path in self.settings.REMOVED_USER_CALLBACKS:
                    callback = import_string(path)
                    callback(user)
                    logger.debug("Called %s for user %s" % (path, username))

        logger.info("Users are synchronized")
