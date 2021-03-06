# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import copy
import fixtures
from newspaper import article as base_article
from newspaper.cleaners import DocumentCleaner
from newspaper import source
from newspaper import urls
from newspaper import utils
from oslo_log import log as logging
from urllib import parse

import datahub.conf
from datahub.news_detector.rule.extractor import VideoExtractor

CONF = datahub.conf.CONF
LOG = logging.getLogger(__name__)


class ArticleException(Exception):
    def __init__(self, *args, **kwargs):
        super(ArticleException, self).__init__(*args, **kwargs)


class Article(base_article.Article):

    def __init__(self, url, title='', source_url='', config=None,
                 extractor=None, **kwargs):
        super(Article, self).__init__(url, title, source_url, config, **kwargs)
        self.title = ''
        self.link_hash = None
        self.extractor = extractor

    def set_meta_language(self, meta_lang):
        """Save langauges in their ISO 2-character form
        """
        self.meta_lang = meta_lang

    def parse(self):
        if not self.is_downloaded:
            raise ArticleException(self)

        self.doc = self.config.get_parser().fromstring(self.html)
        self.clean_doc = copy.deepcopy(self.doc)

        if self.doc is None:
            # `parse` call failed, return nothing
            return

        # TODO(hieulq): Fix this, sync in our fix_url() method
        meta_lang = self.extractor.get_meta_lang(self.clean_doc)
        if CONF.news_detector.language not in meta_lang[0]:
            return

        if self.config.use_meta_language:
            self.extractor.update_language(meta_lang[0])

        parse_candidate = self.get_parse_candidate()
        self.link_hash = parse_candidate.link_hash  # MD5

        document_cleaner = DocumentCleaner(self.config)

        title = self.extractor.get_title(self.clean_doc)
        self.set_title(title)

        authors = self.extractor.get_authors(self.clean_doc)
        self.set_authors(authors)

        meta_favicon = self.extractor.get_favicon(self.clean_doc)
        self.set_meta_favicon(meta_favicon)

        meta_description = \
            self.extractor.get_meta_description(self.clean_doc)
        self.set_meta_description(meta_description)

        canonical_link = self.extractor.get_canonical_link(self.clean_doc)
        self.set_canonical_link(canonical_link)

        tags = self.extractor.extract_tags(self.clean_doc)
        self.set_tags(tags)

        meta_keywords = self.extractor.get_meta_keywords(
            self.clean_doc)
        self.set_meta_keywords(meta_keywords)

        meta_data = self.extractor.get_meta_data(self.clean_doc)
        self.set_meta_data(meta_data)

        self.publish_date = self.extractor.get_publishing_date(
            self.url,
            self.clean_doc)

        # Before any computations on the body, clean DOM object
        self.doc = document_cleaner.clean(self.doc)

        self.top_node = self.extractor.calculate_best_node(self.doc)
        if self.top_node:
            video_extractor = VideoExtractor(self.config, self.top_node)
            self.set_movies(video_extractor.get_videos())
            self.set_text(self.top_node.xpath)

        self.is_parsed = True
        self.release_resources()

    def from_format(self, template):
        if not template or not isinstance(template, Article) or \
                not template.is_parsed or not self.is_downloaded:
            raise ArticleException(self)

        self.doc = self.config.get_parser().fromstring(self.html)
        self.clean_doc = copy.deepcopy(self.doc)

        if self.doc is None:
            # `parse` call failed, return nothing
            raise ArticleException(self)

        parser = self.config.get_parser()
        if template.title:
            res = parser.xpath_re(self.doc, template.title)
            if res:
                self.title = res[0].text

        if template.text:
            texts = parser.xpath_re(self.doc, template.text)
            res = ''
            for text in texts:
                res += text.text + '\n'
            self.text = res

        if template.authors:
            res = parser.xpath_re(self.doc, template.authors[0])
            if res:
                self.authors = res[0].text

        if template.publish_date:
            res = parser.xpath_re(self.doc, template.publish_date)
            if res:
                self.publish_date = res[0].text

        self.is_parsed = True
        self.release_resources()

    def __str__(self):
        return ("==============="
                "Article with:\n"
                "+ URL: %s\n"
                "+ Title: %s\n"
                "+ Author: %s\n"
                "+ Date: %s\n"
                "+ Content: %s\n" % (self.url, self.title, self.authors,
                                     self.publish_date, self.text))

    def process(self):
        self.download()
        self.parse()


class Source(source.Source):

    def __init__(self, url, config=None, extractor=None, **kwargs):
        if (url is None) or ('://' not in url) or (url[:4] != 'http'):
            raise ValueError('Input url is bad!')

        self.config = config
        self.config = utils.extend_config(self.config, kwargs)

        self.extractor = extractor

        self.url = url
        self.url = urls.prepare_url(url)

        self.domain = urls.get_domain(self.url)
        self.scheme = urls.get_scheme(self.url)

        self.categories = []
        self.feeds = []
        self.articles = []

        self.html = ''
        self.doc = None

        self.logo_url = ''
        self.favicon = ''
        self.brand = 'datahub'
        self.description = ''

        self.is_parsed = False
        self.is_downloaded = False

    def set_categories(self):
        targets = self._get_category_urls(self.domain)
        # de-duplicate URL in result
        seen = set()
        targets = [url for url in targets
                   if url not in seen and not seen.add(url)]
        self.categories = [source.Category(url=url) for url in targets]

    def _generate_format_for_categories(self, sampling=1,
                                        process_article=True,
                                        process_all=False):
        categories = self.categories
        articles = self.articles
        candidates = {}
        # Eliminate all articles have same domain, keep only sampling candidate
        for category in categories:
            flag = sampling
            domain_path = parse.urlparse(category.url).path
            domain = parse.urlparse(category.url).netloc + domain_path
            for article in articles:
                a_domain = parse.urlparse(article.url).netloc
                if domain_path:
                    a_domain += "/" + \
                        parse.urlparse(article.url).path.split('/', 2)[1]
                if domain == a_domain:
                    if domain not in candidates:
                        candidates[domain] = []
                    candidates[domain].append(article)
                    flag -= 1
                if flag == 0:
                    break

        # Return unprocess article if process_article=False
        if not process_article:
            return candidates

        # Detect format for each domain
        if process_all:
            for key, values in candidates.items():
                for value in values:
                    try:
                        value.process()
                    except ArticleException:
                        LOG.error("Cannot process article with url %s" %
                                  value.url)
                        continue
        else:
            for domain in list(candidates):
                while True:
                    try:
                        if len(candidates[domain]) > 0:
                            candidates[domain][0].process()
                            if not candidates[domain][0].is_parsed:
                                raise ArticleException(candidates[domain][0])
                        else:
                            break
                    except ArticleException:
                        LOG.error("Cannot process article with url %s" %
                                  candidates[domain][0].url)
                        del candidates[domain][0]
                        if len(candidates[domain]) < 1:
                            del candidates[domain]
                            break
                        continue
                    break

        return candidates

    # def feeds_to_articles(self):
    #     """Returns articles given the url of a feed
    #     """
    #     articles = []
    #     for feed in self.feeds:
    #         urls = self.extractor.get_urls(feed.rss, regex=True)
    #         cur_articles = []
    #
    #         for url in urls:
    #             article = Article(url=url, source_url=self.url,
    #                            config=self.config, extractor=self.extractor)
    #             cur_articles.append(article)
    #
    #         cur_articles = self.purge_articles('url', cur_articles)
    #         articles.extend(cur_articles)
    #
    #     return articles

    def categories_to_articles(self):
        """Takes the categories, splays them into a big list of urls and churns
        the articles out of each url with the url_to_article method
        """
        articles = []
        for category in self.categories:
            cur_articles = []
            url_title_tups = self.extractor.get_urls(category.doc, titles=True)

            for tup in url_title_tups:
                indiv_url = tup[0]
                indiv_title = tup[1]

                _article = Article(url=indiv_url, source_url=self.url,
                                   title=indiv_title, config=self.config,
                                   extractor=self.extractor)
                cur_articles.append(_article)

            cur_articles = self.purge_articles('url', cur_articles)
            articles.extend(cur_articles)

        return articles

    def process(self):
        result = {}
        try:
            self.download()
            self.parse()

            self.set_categories()
            self.download_categories()  # mthread
            self.parse_categories()

            # self.set_feeds()
            # self.download_feeds()  # mthread
            # TODO(hieulq): self.parse_feeds()  # regex for now

            self.generate_articles()
            result = self._generate_format_for_categories()
        except fixtures.TimeoutException:
            LOG.error("Cannot process source with url %s" %
                      self.url)

        return result
