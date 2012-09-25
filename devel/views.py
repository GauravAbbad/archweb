from datetime import timedelta
import operator
import pytz
import time

from django.http import HttpResponseRedirect
from django.contrib.auth.decorators import \
        login_required, permission_required, user_passes_test
from django.contrib.admin.models import LogEntry, ADDITION
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Count, Max
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import filesizeformat
from django.views.decorators.cache import never_cache
from django.utils.encoding import force_unicode
from django.utils.http import http_date
from django.utils.timezone import now

from .forms import ProfileForm, UserProfileForm, NewUserForm
from main.models import Package, PackageFile, TodolistPkg
from main.models import Arch, Repo
from news.models import News
from packages.models import PackageRelation, Signoff, FlagRequest, Depend
from packages.utils import get_signoff_groups
from todolists.utils import get_annotated_todolists
from .utils import get_annotated_maintainers, UserFinder


@login_required
def index(request):
    '''the developer dashboard'''
    if(request.user.is_authenticated()):
        inner_q = PackageRelation.objects.filter(user=request.user)
    else:
        inner_q = PackageRelation.objects.none()
    inner_q = inner_q.values('pkgbase')

    flagged = Package.objects.normal().filter(
            flag_date__isnull=False, pkgbase__in=inner_q).order_by('pkgname')

    todopkgs = TodolistPkg.objects.select_related(
            'pkg', 'pkg__arch', 'pkg__repo').filter(complete=False)
    todopkgs = todopkgs.filter(pkg__pkgbase__in=inner_q).order_by(
            'list__name', 'pkg__pkgname')

    todolists = get_annotated_todolists(incomplete_only=True)

    signoffs = sorted(get_signoff_groups(user=request.user),
            key=operator.attrgetter('pkgbase'))

    arches = Arch.objects.all().annotate(
            total_ct=Count('packages'), flagged_ct=Count('packages__flag_date'))
    repos = Repo.objects.all().annotate(
            total_ct=Count('packages'), flagged_ct=Count('packages__flag_date'))
    # the join is huge unless we do this separately, so merge the result here
    repo_maintainers = dict(Repo.objects.order_by().filter(
            userprofile__user__is_active=True).values_list('id').annotate(
            Count('userprofile')))
    for repo in repos:
        repo.maintainer_ct = repo_maintainers.get(repo.id, 0)

    maintainers = get_annotated_maintainers()

    maintained = PackageRelation.objects.filter(
            type=PackageRelation.MAINTAINER).values('pkgbase')
    total_orphans = Package.objects.exclude(pkgbase__in=maintained).count()
    total_flagged_orphans = Package.objects.filter(
            flag_date__isnull=False).exclude(pkgbase__in=maintained).count()
    total_updated = Package.objects.filter(packager__isnull=True).count()
    orphan = {
            'package_count': total_orphans,
            'flagged_count': total_flagged_orphans,
            'updated_count': total_updated,
    }

    page_dict = {
            'todos': todolists,
            'arches': arches,
            'repos': repos,
            'maintainers': maintainers,
            'orphan': orphan,
            'flagged': flagged,
            'todopkgs': todopkgs,
            'signoffs': signoffs
    }

    return render(request, 'devel/index.html', page_dict)


@login_required
def clock(request):
    devs = User.objects.filter(is_active=True).order_by(
            'first_name', 'last_name').select_related('userprofile')

    latest_news = dict(News.objects.filter(
            author__is_active=True).values_list('author').order_by(
            ).annotate(last_post=Max('postdate')))
    latest_package = dict(Package.objects.filter(
            packager__is_active=True).values_list('packager').order_by(
            ).annotate(last_build=Max('build_date')))
    latest_signoff = dict(Signoff.objects.filter(
            user__is_active=True).values_list('user').order_by(
            ).annotate(last_signoff=Max('created')))
    # The extra() bit ensures we can use our 'user_id IS NOT NULL' index
    latest_flagreq = dict(FlagRequest.objects.filter(
            user__is_active=True).extra(
            where=['user_id IS NOT NULL']).values_list('user_id').order_by(
            ).annotate(last_flagrequest=Max('created')))
    latest_log = dict(LogEntry.objects.filter(
            user__is_active=True).values_list('user').order_by(
            ).annotate(last_log=Max('action_time')))

    for dev in devs:
        dates = [
            latest_news.get(dev.id, None),
            latest_package.get(dev.id, None),
            latest_signoff.get(dev.id, None),
            latest_flagreq.get(dev.id, None),
            latest_log.get(dev.id, None),
            dev.last_login,
        ]
        dates = [d for d in dates if d is not None]
        if dates:
            dev.last_action = max(dates)
        else:
            dev.last_action = None

    current_time = now()
    page_dict = {
            'developers': devs,
            'utc_now': current_time,
    }

    response = render(request, 'devel/clock.html', page_dict)
    if not response.has_header('Expires'):
        expire_time = current_time.replace(second=0, microsecond=0)
        expire_time += timedelta(minutes=1)
        expire_time = time.mktime(expire_time.timetuple())
        response['Expires'] = http_date(expire_time)
    return response


@login_required
@never_cache
def change_profile(request):
    if request.POST:
        form = ProfileForm(request.POST)
        profile_form = UserProfileForm(request.POST, request.FILES,
                instance=request.user.get_profile())
        if form.is_valid() and profile_form.is_valid():
            request.user.email = form.cleaned_data['email']
            if form.cleaned_data['passwd1']:
                request.user.set_password(form.cleaned_data['passwd1'])
            with transaction.commit_on_success():
                request.user.save()
                profile_form.save()
            return HttpResponseRedirect('/devel/')
    else:
        form = ProfileForm(initial={'email': request.user.email})
        profile_form = UserProfileForm(instance=request.user.get_profile())
    return render(request, 'devel/profile.html',
            {'form': form, 'profile_form': profile_form})


@login_required
def report(request, report_name, username=None):
    title = 'Developer Report'
    packages = Package.objects.normal()
    names = attrs = user = None

    if username:
        user = get_object_or_404(User, username=username, is_active=True)
        maintained = PackageRelation.objects.filter(user=user,
                type=PackageRelation.MAINTAINER).values('pkgbase')
        packages = packages.filter(pkgbase__in=maintained)

    maints = User.objects.filter(id__in=PackageRelation.objects.filter(
        type=PackageRelation.MAINTAINER).values('user'))

    if report_name == 'old':
        title = 'Packages last built more than one year ago'
        cutoff = now() - timedelta(days=365)
        packages = packages.filter(
                build_date__lt=cutoff).order_by('build_date')
    elif report_name == 'long-out-of-date':
        title = 'Packages marked out-of-date more than 90 days ago'
        cutoff = now() - timedelta(days=90)
        packages = packages.filter(
                flag_date__lt=cutoff).order_by('flag_date')
    elif report_name == 'big':
        title = 'Packages with compressed size > 50 MiB'
        cutoff = 50 * 1024 * 1024
        packages = packages.filter(
                compressed_size__gte=cutoff).order_by('-compressed_size')
        names = [ 'Compressed Size', 'Installed Size' ]
        attrs = [ 'compressed_size_pretty', 'installed_size_pretty' ]
        # Format the compressed and installed sizes with MB/GB/etc suffixes
        for package in packages:
            package.compressed_size_pretty = filesizeformat(
                package.compressed_size)
            package.installed_size_pretty = filesizeformat(
                package.installed_size)
    elif report_name == 'badcompression':
        title = 'Packages that have little need for compression'
        cutoff = 0.90 * F('installed_size')
        packages = packages.filter(compressed_size__gt=0, installed_size__gt=0,
                compressed_size__gte=cutoff).order_by('-compressed_size')
        names = [ 'Compressed Size', 'Installed Size', 'Ratio', 'Type' ]
        attrs = [ 'compressed_size_pretty', 'installed_size_pretty',
                'ratio', 'compress_type' ]
        # Format the compressed and installed sizes with MB/GB/etc suffixes
        for package in packages:
            package.compressed_size_pretty = filesizeformat(
                package.compressed_size)
            package.installed_size_pretty = filesizeformat(
                package.installed_size)
            ratio = package.compressed_size / float(package.installed_size)
            package.ratio = '%.3f' % ratio
            package.compress_type = package.filename.split('.')[-1]
    elif report_name == 'uncompressed-man':
        title = 'Packages with uncompressed manpages'
        # checking for all '.0'...'.9' + '.n' extensions
        bad_files = PackageFile.objects.filter(is_directory=False,
                directory__contains='/man/',
                filename__regex=r'\.[0-9n]').exclude(
                filename__endswith='.gz').exclude(
                filename__endswith='.xz').exclude(
                filename__endswith='.bz2').exclude(
                filename__endswith='.html')
        if username:
            pkg_ids = set(packages.values_list('id', flat=True))
            bad_files = bad_files.filter(pkg__in=pkg_ids)
        bad_files = bad_files.values_list(
                'pkg_id', flat=True).order_by().distinct()
        packages = packages.filter(id__in=set(bad_files))
    elif report_name == 'uncompressed-info':
        title = 'Packages with uncompressed infopages'
        # we don't worry about looking for '*.info-1', etc., given that an
        # uncompressed root page probably exists in the package anyway
        bad_files = PackageFile.objects.filter(is_directory=False,
                directory__endswith='/info/', filename__endswith='.info')
        if username:
            pkg_ids = set(packages.values_list('id', flat=True))
            bad_files = bad_files.filter(pkg__in=pkg_ids)
        bad_files = bad_files.values_list(
                'pkg_id', flat=True).order_by().distinct()
        packages = packages.filter(id__in=set(bad_files))
    elif report_name == 'unneeded-orphans':
        title = 'Orphan packages required by no other packages'
        owned = PackageRelation.objects.all().values('pkgbase')
        required = Depend.objects.all().values('name')
        # The two separate calls to exclude is required to do the right thing
        packages = packages.exclude(pkgbase__in=owned).exclude(
                pkgname__in=required)
    elif report_name == 'mismatched-signature':
        title = 'Packages with mismatched signatures'
        names = [ 'Signature Date', 'Signed By', 'Packager' ]
        attrs = [ 'sig_date', 'sig_by', 'packager' ]
        cutoff = timedelta(hours=24)
        finder = UserFinder()
        filtered = []
        packages = packages.filter(pgp_signature__isnull=False)
        for package in packages:
            sig_date = package.signature.creation_time.replace(tzinfo=pytz.utc)
            package.sig_date = sig_date.date()
            key_id = package.signature.key_id
            signer = finder.find_by_pgp_key(key_id)
            package.sig_by = signer or key_id
            if signer is None or signer.id != package.packager_id:
                filtered.append(package)
            elif sig_date > package.build_date + cutoff:
                filtered.append(package)
        packages = filtered
    else:
        raise Http404

    arches = set(pkg.arch for pkg in packages)
    repos = set(pkg.repo for pkg in packages)
    context = {
        'all_maintainers': maints,
        'title': title,
        'maintainer': user,
        'packages': packages,
        'arches': sorted(arches),
        'repos': sorted(repos),
        'column_names': names,
        'column_attrs': attrs,
    }
    return render(request, 'devel/packages.html', context)


def log_addition(request, obj):
    """Cribbed from ModelAdmin.log_addition."""
    LogEntry.objects.log_action(
        user_id         = request.user.pk,
        content_type_id = ContentType.objects.get_for_model(obj).pk,
        object_id       = obj.pk,
        object_repr     = force_unicode(obj),
        action_flag     = ADDITION,
        change_message  = "Added via Create New User form."
    )


@permission_required('auth.add_user')
@never_cache
def new_user_form(request):
    if request.POST:
        form = NewUserForm(request.POST)
        if form.is_valid():
            with transaction.commit_on_success():
                form.save()
                log_addition(request, form.instance.user)
            return HttpResponseRedirect('/admin/auth/user/%d/' % \
                    form.instance.user.id)
    else:
        form = NewUserForm()

    context = {
        'description': '''A new user will be created with the
            following properties in their profile. A random password will be
            generated and the user will be e-mailed with their account details
            n plaintext.''',
        'form': form,
        'title': 'Create User',
        'submit_text': 'Create User'
    }
    return render(request, 'general_form.html', context)


@user_passes_test(lambda u: u.is_superuser)
def admin_log(request, username=None):
    user = None
    if username:
        user = get_object_or_404(User, username=username)
    context = {
        'title': "Admin Action Log",
        'log_user':  user,
    }
    return render(request, 'devel/admin_log.html', context)

# vim: set ts=4 sw=4 et:
