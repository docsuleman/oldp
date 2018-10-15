from django.core import serializers
from django.core.serializers.base import DeserializationError
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _

from oldp.apps.courts.models import Court
from oldp.apps.laws.models import *
from oldp.apps.search.models import RelatedContent, SearchableContent

# Get an instance of a logger
logger = logging.getLogger(__name__)


class Case(models.Model, SearchableContent):
    title = models.CharField(
        max_length=255,
        default='',
        blank=True,
        help_text='Title (currently not used due to copyright issues)'
    )
    slug = models.SlugField(
        max_length=200,
        unique=True,
        db_index=True,
        help_text='Used to urls (consists of court, date, file number)',
    )
    court = models.ForeignKey(
        Court,
        default=Court.DEFAULT_ID,
        help_text='Responsible court entity',
        on_delete=models.SET_DEFAULT
    )
    court_raw = models.CharField(
        max_length=255,
        default='{}',
        help_text='Raw court information from crawler (JSON)'
    )  # JSON field
    court_chamber = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        help_text='Court chamber (e.g. 1. Senat)'
    )
    date = models.DateField(
        null=True,
        db_index=True,
        help_text='Publication date as in source'
    )
    created_date = models.DateTimeField(
        auto_now_add=True,
        help_text='Entry is created at this date time'
    )
    updated_date = models.DateTimeField(
        auto_now=True,
        help_text='Date time of last change'
    )
    file_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text='File number as defined by court'
    )
    type = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text='Type of decision (Urteil, Beschluss, ...)'
    )
    pdf_url = models.URLField(
        # TODO Maybe we should store PDF files locally as well
        null=True,
        blank=True,
        max_length=255,
        help_text='URL to original PDF file (not in use)'
    )
    source_url = models.URLField(
        max_length=255,
        help_text='Path to source of crawler'
    )
    source_homepage = models.URLField(
        max_length=200,
        help_text='Link to source homepage'
    )
    source_name = models.CharField(
        max_length=100,
        help_text='Name of source (crawler class)'
    )
    private = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Private content is hidden in production for non-staff users'
    )
    raw = models.TextField(
        null=True,
        blank=True,
        help_text='Raw content (HTML) from crawler that can used to reconstruct all case information'
    )
    abstract = RichTextField(
        null=True,
        blank=True,
        help_text='Case abstract (Leitsatz) formatted in HTML'
    )
    content = RichTextField(
        help_text='Case full-text formatted in Legal HTML'
    )
    annotations = models.TextField(
        blank=True
    )
    ecli = models.CharField(
        max_length=255,
        blank=True,
        help_text='European Case Law Identifier'
    )
    # source_path = None
    reference_markers = None
    references = None

    # Define files that will be excluded in JSON export / Elasticsearch document
    es_fields_exclude = ['content', 'raw']
    es_type = 'case'

    class Meta:
        unique_together = (("court", "file_number"),)

    def is_private(self):
        return self.private

    def get_filename(self, ext='json'):
        return '%s.%s' % (self.slug, ext)

    def get_topics(self):
        # TODO
        return _('Unknown topic')

    def get_court_raw(self):
        return json.loads(self.court_raw)

    def get_relevant_laws(self):
        # TODO
        return []

    def get_references(self):
        """
        Get reference with custom query (grouped by to_hash).
        :return:
        """
        if self.references is None:
            from oldp.apps.references.models import CaseReference, CaseReferenceMarker

            query = '''
              SELECT *, COUNT(*) as `count`
              FROM ''' + CaseReference._meta.db_table + ''' as r, ''' + CaseReferenceMarker._meta.db_table + ''' as m
              WHERE r.marker_id = m.id AND m.referenced_by_id = %(source_id)s
              GROUP BY `to_hash`
              ORDER BY `count` DESC'''
            self.references = CaseReference.objects.raw(query, {'source_id': self.pk})

        # self.references = CaseReference.objects\
        #         .filter(marker__referenced_by=self)\
        #         .annotate(count=Count('to'))\
        #         .order_by('-count')

        return self.references

    def get_reference_markers(self):
        if self.reference_markers is None:
            from oldp.apps.references.models import CaseReferenceMarker
            self.reference_markers = CaseReferenceMarker.objects.filter(referenced_by=self)
        return self.reference_markers

    def get_type(self):
        return self.__class__.__name__

    def get_id(self):
        return self.id

    def get_content_as_html(self):
        # return markdown.markdown(self.content, extensions=[
        #     'legal_md.extensions.line_numbers',
        # ])
        return self.content

    def get_text(self) -> str:
        """ Case content as plain text

        :return: plain-text
        """

        # if self.text != '':
        #     return self.text

        # raise NotImplementedError('get_text missing')
        return strip_tags(self.content)

    def get_source_url(self) -> str:
        return self.source_url

    def get_title(self) -> str:

        try:
            court_name = self.court.name

            # Attach chamber if available
            if self.court_chamber is not None and self.court_chamber != '':
                court_name += ' (' + self.court_chamber + ')'
        except Court.DoesNotExist:
            court_name = '(no court)'

        return '%s vom %s - %s' % (self.get_case_type(), court_name, self.file_number)

    def get_short_title(self, max_length=75) -> str:
        title = self.get_title()
        if len(title) > max_length:
            return title[:max_length] + '...'
        else:
            return title

    def get_case_type(self):
        return self.type

    def get_date(self, date_format='%Y-%m-%d'):
        return self.date.strftime(date_format)

    def get_related(self, n=5):
        """
        Related items that are pre-computed with "generate_related_cases" command.

        :param n: number of items
        :return:
        """
        items = []
        for item in RelatedCase.objects.filter(seed_content=self).order_by('-score')[:n]:
            items.append(item.related_content)
        return items

    def get_absolute_url(self):
        if self.slug is None or self.slug == '':
            self.slug = 'no-slug'

        return reverse('cases:case', args=(self.slug,))

    def get_admin_url(self):
        return reverse('admin:cases_case_change', args=(self.pk, ))

    def get_es_url(self):
        return settings.ELASTICSEARCH_URL + '/modelresult/cases.case.%s' % self.pk

    def get_search_snippet(self, max_length=100):
        if self.search_snippet is None:
            text = self.get_text()

            from oldp.apps.references.models import CaseReferenceMarker
            text = CaseReferenceMarker.remove_markers(text)

            return text[:max_length]
        else:
            return self.search_snippet

    def set_slug(self):
        # Transform date to string
        if isinstance(self.date, datetime.date):
            date_str = self.date.strftime('%Y-%m-%d')
        else:
            date_str = '%s' % self.date

        # File numbers can be lists, so limit the length
        max_fn_length = 20

        self.slug = self.court.slug + '-' + date_str+ '-' + slugify(self.file_number[:max_fn_length])

    def set_ecli(self):
        """Generate ECLI from court code and file number

        See ECLI definition:

        Consists of:
        - ‘ECLI’: to identify the identifier as being a European Case Law Identifier;
        - the country code;
        - the code of the court that rendered the judgment;
        - the year the judgment was rendered;
        - an ordinal number, up to 25 alphanumeric characters, in a format that is decided upon by each Member State.
            Dots are allowed, but not other punctuation marks.

        """
        self.ecli = 'ECLI:de:' + self.court.code + ':' + str(self.date.year) + ':' + slugify(self.file_number)

    def save_reference_markers(self):
        """
        Save references markers generated by ExtractRefs processing step

        :return: None
        """
        from oldp.apps.references.models import CaseReferenceMarker

        if self.reference_markers:
            for ref in self.reference_markers:
                marker = CaseReferenceMarker().from_ref(ref, self)
                marker.save()
                # logger.debug('Saved: %s' % marker)

                marker.set_references(marker.ids)
                marker.save_references()

        else:
            # logger.debug('No reference markers to save')
            pass

    def __str__(self):
        return 'Case(title=%s, file_number=%s)' % (self.get_title(), self.file_number)

    def to_json(self, file_path=None) -> str:
        json_str = serializers.serialize("json", [self])

        if file_path is not None:
            with open(file_path, 'w') as f:
                f.write(json_str)

        return json_str

    @staticmethod
    def from_json_file(file_path):
        with open(file_path) as f:
            out = serializers.deserialize("json", f.read())  # , ignorenonexistent=True)
            # print(len(out))

            try:
                for o in out:
                    return o.object
            except DeserializationError:
                pass

            raise ValueError('Cannot deserialize: %s' % file_path)

        # MySQL utf8mb4 bugfix
        # if instance.raw is not None:
        #     instance.raw = ''.join([char if ord(char) < 128 else '' for char in instance.raw])
        #
        # if instance.text is not None:
        #     instance.text = ''.join([char if ord(char) < 128 else '' for char in instance.text])
        #
        # return instance

    @staticmethod
    def get_queryset(request=None):
        # TODO superuser?
        if settings.DEBUG:
            return Case.objects.all()
        else:
            # production
            # hide private content
            return Case.objects.filter(private=False)


class RelatedCase(RelatedContent):
    seed_content = models.ForeignKey(Case, related_name='seed_id', on_delete=models.CASCADE)
    related_content = models.ForeignKey(Case, related_name='related_id', on_delete=models.CASCADE)

