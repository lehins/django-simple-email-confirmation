from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models.signals import post_save
from django.utils.crypto import get_random_string
from django.utils import timezone

from .exceptions import (
    EmailConfirmationExpired, EmailIsPrimary, EmailNotConfirmed,
)
from .signals import (
    email_confirmed, unconfirmed_email_created, primary_email_changed,
)


class SimpleEmailConfirmationUserMixin(object):
    """
    Mixin to be used with your django 1.5+ custom User model.
    Provides python-level functionality only.
    """

    # if your User object stores the User's primary email address
    # in a place other than User.email, you can override the
    # primary_email_field_name and/or primary_email get/set methods.
    # All access to a User's primary_email in this app passes through
    # these two get/set methods.

    primary_email_field_name = 'email'

    def get_primary_email(self):
        return getattr(self, self.primary_email_field_name)

    def set_primary_email(self, email, require_confirmed=True):
        "Set an email address as primary"
        old_email = self.get_primary_email()
        if email == old_email:
            return

        if email not in self.confirmed_emails and require_confirmed:
            raise EmailNotConfirmed()

        setattr(self, self.primary_email_field_name, email)
        self.save(update_fields=[self.primary_email_field_name])
        primary_email_changed.send(
            sender=self, old_email=old_email, new_email=email,
        )

    @property
    def is_confirmed(self):
        "Is the User's primary email address confirmed?"
        return self.get_primary_email() in self.confirmed_emails

    @property
    def confirmed_at(self):
        "When the User's primary email address was confirmed, or None"
        address = self.email_address_set.get(email=self.get_primary_email())
        return address.confirmed_at

    @property
    def confirmation_key(self):
        """
        Confirmation key for the User's primary email

        DEPRECATED. Use get_confirmation_key() instead.
        """
        email = self.get_primary_email()
        return self.get_confirmation_key(email)

    @property
    def confirmed_emails(self):
        "DEPRECATED. Use get_confirmed_emails() instead."
        return self.get_confirmed_emails()

    @property
    def unconfirmed_emails(self):
        "DEPRECATED. Use get_unconfirmed_emails() instead."
        return self.get_unconfirmed_emails()

    def get_confirmation_key(self, email=None):
        "Get the confirmation key for an email"
        email = email or self.get_primary_email()
        address = self.email_address_set.get(email=email)
        return address.key

    def get_confirmed_emails(self):
        "List of emails this User has confirmed"
        address_qs = self.email_address_set.filter(confirmed_at__isnull=False)
        return [address.email for address in address_qs]

    def get_unconfirmed_emails(self):
        "List of emails this User has been associated with but not confirmed"
        address_qs = self.email_address_set.filter(confirmed_at__isnull=True)
        return [address.email for address in address_qs]

    def confirm_email(self, confirmation_key, save=True):
        """
        Attempt to confirm an email using the given key.
        Returns the email that was confirmed, or raise an exception.
        """
        address = self.email_address_set.confirm(confirmation_key, save=save)
        return address.email

    def add_confirmed_email(self, email):
        "Adds an email to the user that's already in the confirmed state"
        # if email already exists, let exception be thrown
        address = self.email_address_set.create_confirmed(email)
        return address.key

    def add_unconfirmed_email(self, email, key_length=None):
        "Adds an unconfirmed email address and returns it's confirmation key"
        # if email already exists, let exception be thrown
        address = self.email_address_set.create_unconfirmed(
            email, key_length=key_length)
        return address.key

    def add_email_if_not_exists(self, email):
        """
        If the user already has the email, and it's confirmed, do nothing
        and return None.

        If the user already has the email, and it's unconfirmed, reset the
        confirmation. If the confirmation is unexpired, do nothing. Return
        the confirmation key of the email.
        """
        try:
            address = self.email_address_set.get(email=email)
        except EmailAddress.DoesNotExist:
            key = self.add_unconfirmed_email(email)
        else:
            if not address.is_confirmed:
                key = address.reset_confirmation()
            else:
                key = None

        return key

    def reset_email_confirmation(self, email):
        "Reset the expiration of an email confirmation"
        address = self.email_address_set.get(email=email)
        return address.reset_confirmation()

    def remove_email(self, email):
        "Remove an email address"
        # if email already exists, let exception be thrown
        if email == self.get_primary_email():
            raise EmailIsPrimary()
        address = self.email_address_set.get(email=email)
        address.delete()


class EmailAddressManager(models.Manager):

    def generate_key(self, key_length=None):
        "Generate a new random key and return it"
        # sticking with the django defaults
        if key_length is None:
            return get_random_string()
        length = min(key_length, 40) # make sure it fits in the field
        return get_random_string(length=length)

    def create_confirmed(self, email, user=None):
        "Create an email confirmation obj from the given email address obj"
        user = user or getattr(self, 'instance', None)
        if not user:
            raise ValueError('Must specify user or call from related manager')
        now = timezone.now()
        # let email-already-exists exception propogate through
        return self.create(
            user=user, email=email, set_at=now, confirmed_at=now, key=self.generate_key()
        )

    def create_unconfirmed(self, email, user=None, key_length=None):
        "Create an email address in the unconfirmed state"
        user = user or getattr(self, 'instance', None)
        if not user:
            raise ValueError('Must specify user or call from related manager')
        key = self.generate_key(key_length=key_length)
        # let email-already-exists exception propogate through
        address = self.create(user=user, email=email, key=key)
        unconfirmed_email_created.send(sender=user, email=email)
        return address

    def confirm(self, key, user=None, save=True):
        "Confirm an email address. Returns the address that was confirmed."
        queryset = self.all()
        if user:
            queryset = queryset.filter(user=user)
        address = queryset.get(key=key)

        if address.is_key_expired:
            raise EmailConfirmationExpired()

        if not address.is_confirmed:
            address.confirmed_at = timezone.now()
            if save:
                address.save(update_fields=['confirmed_at'])
                email_confirmed.send(sender=address.user, email=address.email)

        return address


class EmailAddress(models.Model):
    "An email address belonging to a User"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name='email_address_set',
    )
    email = models.EmailField(max_length=255)
    key = models.CharField(max_length=40, unique=True)

    set_at = models.DateTimeField(
        default=lambda: timezone.now(),
        help_text='When the confirmation key expiration was set',
    )
    confirmed_at = models.DateTimeField(
        blank=True, null=True,
        help_text='First time this email was confirmed',
    )

    objects = EmailAddressManager()

    class Meta:
        unique_together = (('user', 'email'),)

    def __unicode__(self):
        return u'{} <{}>'.format(self.user, self.email)

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None

    @property
    def is_primary(self):
        return bool(self.user.email == self.email)

    @property
    def key_expires_at(self):
        # By default, keys don't expire. If you want them to, set
        # settings.SIMPLE_EMAIL_CONFIRMATION_PERIOD to a timedelta.
        period = getattr(
            settings, 'SIMPLE_EMAIL_CONFIRMATION_PERIOD', None
        )
        return self.set_at + period if period is not None else None

    @property
    def is_key_expired(self):
        return self.key_expires_at and timezone.now() >= self.key_expires_at

    def reset_confirmation(self):
        """
        Re-generate the confirmation key and key expiration associated
        with this email.  Note that the previous confirmation key will
        cease to work.
        """
        self.key = self._default_manager.generate_key()
        self.set_at = timezone.now()

        self.confirmed_at = None
        self.save(update_fields=['key', 'set_at', 'confirmed_at'])
        return self.key


# by default, auto-add unconfirmed EmailAddress objects for new Users
if getattr(settings, 'SIMPLE_EMAIL_CONFIRMATION_AUTO_ADD', True):
    def auto_add(sender, **kwargs):
        if sender == get_user_model() and kwargs['created']:
            user = kwargs.get('instance')
            email = user.get_primary_email()
            user.add_unconfirmed_email(email)

    # TODO: try to only connect this to the User model. We can't use
    #       get_user_model() here - results in import loop.

    post_save.connect(auto_add)
