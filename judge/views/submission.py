from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.db.models import F
from django.http import Http404, HttpResponseRedirect, HttpResponseBadRequest, HttpResponse
from django.shortcuts import render_to_response
from django.template import RequestContext, loader
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.views.generic import TemplateView, ListView
from django.views.generic.detail import SingleObjectMixin

from judge.highlight_code import highlight_code
from judge.models import Problem, Submission, SubmissionTestCase, Profile
from judge.utils.problems import user_completed_ids, get_result_table
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.views import TitleMixin
from judge import event_poster as event


class SubmissionMixin(object):
    model = Submission


class SubmissionDetailBase(TitleMixin, SubmissionMixin, SingleObjectMixin, TemplateView):
    def get(self, request, *args, **kwargs):
        self.object = submission = self.get_object()

        if not request.user.is_authenticated():
            raise PermissionDenied()

        if not request.user.profile.is_admin and submission.user != request.user.profile and \
                not Submission.objects.filter(user=request.user.profile, result='AC',
                                              problem__code=submission.problem.code,
                                              points=F('problem__points')).exists():
            raise PermissionDenied()

        return super(SubmissionDetailBase, self).get(request, *args, submission=self.object, **kwargs)

    def get_title(self):
        submission = self.object
        return 'Submission of %s by %s' % (submission.problem.name, submission.user.user.username)


class SubmissionSource(SubmissionDetailBase):
    template_name = 'submission/source.jade'

    def get_context_data(self, **kwargs):
        context = super(SubmissionSource, self).get_context_data(**kwargs)
        submission = self.object
        context['raw_source'] = submission.source
        context['highlighted_source'] = highlight_code(submission.source, submission.language.pygments)
        return context


class SubmissionStatus(SubmissionDetailBase):
    template_name = 'submission/status.jade'

    def get_context_data(self, **kwargs):
        context = super(SubmissionStatus, self).get_context_data(**kwargs)
        submission = self.object
        context['last_msg'] = event.last()
        context['test_cases'] = submission.test_cases.all()
        return context


def abort_submission(request, code):
    if request.method != 'POST':
        raise Http404()
    submission = Submission.objects.get(id=int(code))
    if not request.user.is_authenticated() or (
                    request.user.profile != submission.user and not request.user.profile.is_admin):
        raise PermissionDenied()
    submission.abort()
    return HttpResponseRedirect(reverse('submission_status', args=(code,)))


class SubmissionsListBase(TitleMixin, ListView):
    model = Submission
    paginate_by = 50
    show_problem = True
    title = 'All submissions'
    template_name = 'submission/list.jade'

    def get_paginator(self, queryset, per_page, orphans=0,
                      allow_empty_first_page=True, **kwargs):
        return DiggPaginator(queryset, per_page, body=6, padding=2,
                             orphans=orphans, allow_empty_first_page=allow_empty_first_page, **kwargs)

    def get_result_table(self):
        return get_result_table(self.get_queryset())

    @cached_property
    def in_contest(self):
        return self.request.user.is_authenticated() and self.request.user.profile.contest.current is not None

    def get_queryset(self):
        queryset = Submission.objects.order_by('-id').defer('source', 'error')
        if self.in_contest:
            return queryset.filter(
                contest__participation__contest_id=self.request.user.profile.contest.current.contest_id
            )
        else:
            return queryset

    def get_context_data(self, **kwargs):
        context = super(SubmissionsListBase, self).get_context_data(**kwargs)
        context['dynamic_update'] = False
        context['show_problem'] = self.show_problem
        context['completed_problem_ids'] = (user_completed_ids(self.request.user.profile)
                                            if self.request.user.is_authenticated() else [])
        context['results'] = self.get_result_table()
        context['submissions'] = context['page_obj']
        return context


class UserMixin(object):
    def get(self, request, *args, **kwargs):
        if 'user' not in kwargs:
            raise ImproperlyConfigured('Must pass a user')
        try:
            self.profile = Profile.objects.get(user__username=kwargs['user'])
        except Profile.DoesNotExist:
            raise Http404()
        else:
            self.username = kwargs['user']
        return super(UserMixin, self).get(request, *args, **kwargs)


class AllUserSubmissions(UserMixin, SubmissionsListBase):
    def get_queryset(self):
        return super(AllUserSubmissions, self).get_queryset().filter(user_id=self.profile.id)

    def get_title(self):
        return 'All submissions by %s' % self.username

    def get_content_title(self):
        return format_html('All submissions by <a href="{1}">{0}</a>', self.username,
                           reverse('judge.views.user', args=[self.username]))


class ProblemSubmissions(SubmissionsListBase):
    show_problem = False

    def get_queryset(self):
        return super(ProblemSubmissions, self).get_queryset().filter(problem__code=self.problem.code)

    def get_title(self):
        return 'All submissions for %s' % self.problem.name

    def get_content_title(self):
        return format_html('All submissions for <a href="{1}">{0}</a>', self.problem.name,
                           reverse('judge.views.problem', args=[self.problem.code]))

    def get(self, request, *args, **kwargs):
        if 'problem' not in kwargs:
            raise ImproperlyConfigured('Must pass a problem')
        try:
            self.problem = Problem.objects.get(code=kwargs['problem'])
        except Problem.DoesNotExist:
            raise Http404()
        return super(ProblemSubmissions, self).get(request, *args, **kwargs)


class UserProblemSubmissions(UserMixin, ProblemSubmissions):
    def get_queryset(self):
        return super(UserProblemSubmissions, self).get_queryset().filter(user_id=self.profile.id)

    def get_title(self):
        return "%s's submissions for %s" % (self.username, self.problem.name)

    def get_content_title(self):
        return format_html('''<a href="{1}">{0}</a>'s submissions for <a href="{3}">{2}</a>''',
                           self.username, reverse('judge.views.user', args=[self.username]),
                           self.problem.name, reverse('judge.views.problem', args=[self.problem.code]))


def single_submission(request, id):
    try:
        authenticated = request.user.is_authenticated()
        return render_to_response('submission/row.jade', {
            'submission': Submission.objects.get(id=int(id)),
            'completed_problem_ids': user_completed_ids(request.user.profile) if authenticated else [],
            'show_problem': True,
            'profile_id': request.user.profile.id if authenticated else 0,
        }, context_instance=RequestContext(request))
    except ObjectDoesNotExist:
        raise Http404()


def submission_testcases_query(request):
    if 'id' not in request.GET or not request.GET['id'].isdigit():
        return HttpResponseBadRequest()
    try:
        submission = Submission.objects.get(id=int(request.GET['id']))
        test_cases = SubmissionTestCase.objects.filter(submission=submission)
        return render_to_response('submission/status_testcases.jade', {
            'submission': submission, 'test_cases': test_cases
        }, context_instance=RequestContext(request))
    except ObjectDoesNotExist:
        raise Http404()


def statistics_table_query(request):
    page = cache.get('sub_stats_table')
    if page is None:
        page = loader.render_to_string('problem/statistics_table.jade', {'results': get_result_table()})
        cache.set('sub_stats_table', page, 86400)
    return HttpResponse(page)


def single_submission_query(request):
    if 'id' not in request.GET or not request.GET['id'].isdigit():
        return HttpResponseBadRequest()
    return single_submission(request, int(request.GET['id']))


class AllSubmissions(SubmissionsListBase):
    def get_result_table(self):
        if self.in_contest:
            return super(AllSubmissions, self).get_result_table()
        results = cache.get('sub_stats_data')
        if results is not None:
            return results
        results = super(AllSubmissions, self).get_result_table()
        cache.set('sub_stats_data', results, 86400)
        return results

    def get_context_data(self, **kwargs):
        context = super(AllSubmissions, self).get_context_data(**kwargs)
        context['dynamic_update'] = context['page_obj'].number == 1
        context['last_msg'] = event.last()
        return context
