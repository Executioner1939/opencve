import json

import pyparsing as pp
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import models
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import DetailView, ListView, TemplateView

from cves.constants import PRODUCT_SEPARATOR
from cves.forms import SearchForm
from cves.models import Cve, Product, Variable, Vendor, Weakness
from cves.search import Search, BadQueryException, MaxFieldsExceededException
from cves.templatetags.opencve_extras import needs_quotes
from cves.utils import humanize, list_to_dict_vendors, list_weaknesses
from opencve.utils import is_valid_uuid
from organizations.mixins import OrganizationRequiredMixin
from projects.models import Project
from users.models import CveTag, UserTag
from views.forms import ViewForm
from views.models import View


class WeaknessListView(ListView):
    context_object_name = "weaknesses"
    template_name = "cves/weakness_list.html"
    paginate_by = 20

    def get_queryset(self):
        query = Weakness.objects
        if self.request.GET.get("search"):
            query = query.filter(name__icontains=self.request.GET.get("search"))
        return query.order_by("-name")


class VendorListView(ListView):
    context_object_name = "vendors"
    template_name = "cves/vendor_list.html"
    paginate_by = 20

    def get_queryset(self):
        vendors = Vendor.objects.order_by("name").prefetch_related("products")
        search = self.request.GET.get("search", "").lower()
        if search:
            vendors = vendors.filter(name__contains=search)
        return vendors

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get all products or filter them by vendor
        vendor = self.request.GET.get("vendor", "").lower()
        products = Product.objects.order_by("name").select_related("vendor")

        # Filter by vendor
        if vendor:
            products = products.filter(vendor__name=vendor)

        # Filter by keyword
        search = self.request.GET.get("search", "").lower()
        if search:
            products = products.filter(name__contains=search)

        # Add the pagination
        paginator = Paginator(products, 20)
        page_number = self.request.GET.get("product_page")
        context["products"] = paginator.get_page(page_number)
        context["paginator_products"] = paginator

        return context


class CveListView(ListView):
    context_object_name = "cves"
    template_name = "cves/cve_list.html"
    paginate_by = 20

    def get_queryset(self):
        self.form = SearchForm(self.request.GET)

        # Validate the form
        search_query = None
        if not self.form.is_valid():
            return Cve.objects.order_by("-updated_at").all()

        # Convert simple search to advanced search
        search_query = self.form.cleaned_data["q"]
        if not search_query:
            search_query = self.convert_to_advanced_search()
            mutable_get = self.request.GET.copy()
            mutable_get["q"] = search_query

            self.form = SearchForm(mutable_get)
            self.form.is_valid()

        # Execute the search
        try:
            search = Search(search_query, self.request)
            return search.query
        except (BadQueryException, MaxFieldsExceededException) as e:
            self.form.add_error("q", e)
        except pp.ParseException as e:
            pass

        return Cve.objects.order_by("-updated_at").all()

    def convert_to_advanced_search(self):
        """
        Converts simple GET parameters to an advanced search query string.
        """
        advanced_parts = []
        vendor_value = self.request.GET.get("vendor")
        product_value = self.request.GET.get("product")
        search_value = self.request.GET.get("search")
        weakness_value = self.request.GET.get("weakness")
        tag_value = self.request.GET.get("tag")

        if vendor_value:
            if needs_quotes(vendor_value):
                vendor_value = f"'{vendor_value}'"
            advanced_parts.append(f"vendor:{vendor_value}")

            if product_value:
                if needs_quotes(product_value):
                    product_value = f"'{product_value}'"
                advanced_parts.append(f"product:{product_value}")

        if weakness_value:
            advanced_parts.append(f"cwe:{weakness_value}")

        if tag_value and self.request.user.is_authenticated:
            advanced_parts.append(f"userTag:{tag_value}")

        if search_value:
            # If multiple words, search in description as a phrase
            if " " in search_value:
                advanced_parts.append(f'description:"{search_value}"')

            # If single word, append directly (parser handles it)
            else:
                advanced_parts.append(search_value)

        return " AND ".join(advanced_parts)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Handle custom titles when vendor, product or weakness is set
        vendor = self.request.GET.get("vendor", "").replace(" ", "").lower()
        product = self.request.GET.get("product", "").replace(" ", "_").lower()
        weakness = self.request.GET.get("weakness", "")

        if weakness:
            context["title"] = weakness

        if vendor:
            context["title"] = humanize(vendor)

        if product:
            context["title"] = humanize(product)

        if self.request.user.is_authenticated:

            # List the user tags
            context["user_tags"] = [
                t.name for t in UserTag.objects.filter(user=self.request.user).all()
            ]

            # List the projects
            context["projects"] = Project.objects.filter(
                organization=self.request.current_organization
            ).order_by("name")

        # Provide the search form
        context["search_form"] = self.form

        # Add organization views
        if self.request.current_organization:
            context["view_form"] = ViewForm(request=self.request)

            context["views"] = View.objects.filter(
                models.Q(
                    privacy="public", organization=self.request.current_organization
                )
                | models.Q(
                    privacy="private",
                    user=self.request.user,
                    organization=self.request.current_organization,
                )
            ).order_by("-created_at")

        return context


class CveDetailView(DetailView):
    model = Cve
    slug_field = "cve_id"
    slug_url_kwarg = "cve_id"
    template_name = "cves/cve_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Raw json files
        context["nvd_json"] = json.dumps(context["cve"].nvd_json)
        context["mitre_json"] = json.dumps(context["cve"].mitre_json)
        context["redhat_json"] = json.dumps(context["cve"].redhat_json)
        context["vulnrichment_json"] = json.dumps(context["cve"].vulnrichment_json)
        context["enrichment_json"] = json.dumps(context["cve"].enrichment_json)

        # Add the associated vendors and weaknesses
        context["vendors"] = list_to_dict_vendors(context["cve"].vendors)
        context["weaknesses"] = list_weaknesses(context["cve"].weaknesses)

        context["enrichment_vendors"] = list_to_dict_vendors(
            context["cve"].enrichment_json.get("vendors", [])
        )

        # Get the CVE tags for the authenticated user
        user_tags = {}
        tags = []

        user = self.request.user
        if user.is_authenticated:
            user_tags = {
                t.name: {"name": t.name, "color": t.color, "description": t.description}
                for t in UserTag.objects.filter(user=self.request.user).all()
            }
            cve_tags = CveTag.objects.filter(
                user=self.request.user, cve=context["cve"]
            ).first()
            if cve_tags:
                tags = [user_tags[cve_tag] for cve_tag in cve_tags.tags]

                # We have to pass an encoded list of tags for the modal box
                context["cve_tags_encoded"] = json.dumps(cve_tags.tags)

        context["user_tags"] = user_tags.keys()
        context["tags"] = tags
        return context

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise Http404()

        cve = self.get_object()
        new_tags = request.POST.getlist("tags", [])

        # Check if all tags are declared by the user
        user_tags = [t.name for t in UserTag.objects.filter(user=request.user).all()]
        for new_tag in new_tags:
            if new_tag not in user_tags:
                raise Http404()

        # Update the CVE tags
        cve_tag = CveTag.objects.filter(user=request.user, cve_id=cve.id).first()
        if not cve_tag:
            cve_tag = CveTag(user=request.user, cve_id=cve.id)
        cve_tag.tags = new_tags
        cve_tag.save()

        return redirect("cve", cve_id=cve.cve_id)


class SubscriptionView(LoginRequiredMixin, OrganizationRequiredMixin, TemplateView):
    template_name = "cves/vendor_subscribe.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vendor_name = self.request.GET.get("vendor")
        product_name = self.request.GET.get("product")

        # The vendor at least is mandatory
        if not vendor_name:
            raise Http404()

        # Get the vendor data
        vendor = get_object_or_404(Vendor, name=vendor_name)
        obj = vendor
        obj_type = "vendor"
        obj_name = obj.name

        # Get the product data
        if product_name:
            product = get_object_or_404(Product, name=product_name, vendor=vendor)
            obj = product
            obj_type = "product"
            obj_name = f"{vendor.name}{PRODUCT_SEPARATOR}{product.name}"

        # Update the context
        context.update(
            **{
                "object": obj,
                "object_type": obj_type,
                "object_name": obj_name,
                "projects": Project.objects.filter(
                    organization=self.request.current_organization
                )
                .order_by("name")
                .all(),
            }
        )

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        obj_type = request.POST.get("obj_type")
        obj_id = request.POST.get("obj_id")
        project_id = request.POST.get("project_id")

        if (
            not all([action, obj_type, obj_id, project_id])
            or not is_valid_uuid(obj_id)
            or not is_valid_uuid(project_id)
            or action not in ["subscribe", "unsubscribe"]
            or obj_type not in ["vendor", "product"]
        ):
            raise Http404()

        # Check if the project belongs to the current organization
        project = get_object_or_404(
            Project, id=project_id, organization=request.current_organization
        )

        # Vendor subscription
        if obj_type == "vendor":
            vendor = get_object_or_404(Vendor, id=obj_id)
            project_vendors = set(project.subscriptions.get("vendors"))

            if action == "subscribe":
                project_vendors.add(vendor.name)
            else:
                try:
                    project_vendors.remove(vendor.name)
                except KeyError:
                    raise Http404()

            project.subscriptions["vendors"] = list(project_vendors)
            project.save()

        if obj_type == "product":
            product = get_object_or_404(Product, id=obj_id)
            project_products = set(project.subscriptions.get("products"))

            if action == "subscribe":
                project_products.add(product.vendored_name)
            else:
                try:
                    project_products.remove(product.vendored_name)
                except KeyError:
                    raise Http404()

            project.subscriptions["products"] = list(project_products)
            project.save()

        return JsonResponse({"status": "ok"})


class StatisticsView(TemplateView):
    template_name = "cves/statistics.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        variables = Variable.objects.filter(name__startswith="statistics").all()
        unserialized_vars = ["statistics_cves_count_last_days"]

        serialized_statistics = {
            v.name: json.dumps(v.value)
            for v in variables
            if v.name not in unserialized_vars
        }
        unserialized_statistics = {
            v.name: v.value for v in variables if v.name in unserialized_vars
        }

        context["serialized_statistics"] = serialized_statistics
        context["cves_count_last_days"] = unserialized_statistics[
            "statistics_cves_count_last_days"
        ]

        return context


def handle_page_not_found(request, exception):
    return render(request, "404.html", status=404)


def handle_server_error(request):
    return render(request, "500.html", status=500)
