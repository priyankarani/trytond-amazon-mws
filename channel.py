# -*- coding: utf-8 -*-
"""
    channle.py

"""
import time
import unicodecsv as csv
import logging
from datetime import datetime
from mws import mws
from lxml import etree
from lxml.builder import E
from dateutil.relativedelta import relativedelta
from StringIO import StringIO

from trytond.model import ModelView, fields
from trytond.wizard import Wizard, StateView, Button
from trytond.transaction import Transaction
from trytond.pyson import Eval
from trytond.pool import Pool, PoolMeta

__metaclass__ = PoolMeta

__all__ = [
    'SaleChannel', 'CheckAmazonServiceStatusView', 'CheckAmazonServiceStatus',
    'CheckAmazonSettingsView', 'CheckAmazonSettings'
]

AMAZON_MWS_STATES = {
    'required': Eval('source') == 'amazon_mws',
    'invisible': ~(Eval('source') == 'amazon_mws')
}

logger = logging.getLogger("amazon_mws")


def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]


class SaleChannel:
    "Amazon MWS Account"
    __name__ = 'sale.channel'

    # These are the credentials that you receive when you register a seller
    # account with Amazon MWS
    amazon_merchant_id = fields.Char(
        "Merchant ID", states=AMAZON_MWS_STATES, depends=['source']
    )
    amazon_marketplace_id = fields.Char(
        "MarketPlace ID", states=AMAZON_MWS_STATES, depends=['source']
    )
    amazon_access_key = fields.Char(
        "Access Key", states=AMAZON_MWS_STATES, depends=['source']
    )
    amazon_secret_key = fields.Char(
        "Secret Key", states=AMAZON_MWS_STATES, depends=['source']
    )
    fba_warehouse = fields.Many2One(
        'stock.location', 'Warehouse (Fulfilled By Amazon)',
        domain=[('type', '=', 'warehouse')],
        states=AMAZON_MWS_STATES, depends=['source']
    )

    @classmethod
    def get_source(cls):
        """
        Get the source
        """
        sources = super(SaleChannel, cls).get_source()

        sources.append(('amazon_mws', 'Amazon Marketplace'))

        return sources

    @classmethod
    def __setup__(cls):
        """
        Setup the class before adding to pool
        """
        super(SaleChannel, cls).__setup__()
        cls._buttons.update({
            'check_amazon_service_status': {},
            'check_amazon_settings': {},
        })

        cls._error_messages.update({
            "missing_product_codes": (
                'Product "%(product)s" misses Amazon Product Identifiers'
            ),
            "missing_product_code": (
                'Product "%(product)s" misses Product Code'
            ),
            'invalid_channel': 'Channel does not belong to Amazon.'
        })

    def validate_amazon_channel(self):
        """
        Check if channel belongs to amazon mws
        """
        if self.source != 'amazon_mws':
            self.raise_user_error('invalid_channel')

    def get_mws_api(self):
        """
        Create an instance of mws api

        :return: mws api instance
        """
        return mws.MWS(
            access_key=self.amazon_access_key,
            secret_key=self.amazon_secret_key,
            account_id=self.amazon_merchant_id,
        )

    def get_mws_report_api(self):
        """
        Create an instance of Report api

        :return: report api instance
        """
        return mws.Reports(
            access_key=self.amazon_access_key,
            secret_key=self.amazon_secret_key,
            account_id=self.amazon_merchant_id,
        )

    def get_amazon_order_api(self):
        """
        Create an instance of Order api

        :return: order api instance
        """
        return mws.Orders(
            access_key=self.amazon_access_key,
            secret_key=self.amazon_secret_key,
            account_id=self.amazon_merchant_id,
        )

    def get_amazon_product_api(self):
        """
        Create an instance of product api

        :return: Product API instance
        """
        return mws.Products(
            access_key=self.amazon_access_key,
            secret_key=self.amazon_secret_key,
            account_id=self.amazon_merchant_id,
        )

    def get_amazon_feed_api(self):
        """
        Return an instance of feed api
        """
        return mws.Feeds(
            access_key=self.amazon_access_key,
            secret_key=self.amazon_secret_key,
            account_id=self.amazon_merchant_id,
        )

    @classmethod
    @ModelView.button_action('amazon_mws.check_amazon_service_status')
    def check_amazon_service_status(cls, channels):
        """
        Check GREEN, GREEN_I, YELLOW or RED status

        :param channels: Active record list of sale channels
        """
        pass

    @classmethod
    @ModelView.button_action('amazon_mws.check_amazon_settings')
    def check_amazon_settings(cls, channels):
        """
        Checks account settings configured

        :param accounts: Active record list of sale channels
        """
        pass

    def import_orders(self):
        """
        Downstream implementation of channel.import_orders
        :return: List of active record of sale imported
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).import_orders()

        Date = Pool().get('ir.date')

        order_api = self.get_amazon_order_api()

        created_after = (
            Date.today() - relativedelta(months=1)
        ).strftime('%Y-%m-%dT00:00:01Z')
        response = order_api.list_orders(
            marketplaceids=[self.amazon_marketplace_id],
            created_after=created_after,
            # Unshipped and PartiallyShipped must be used together in
            # this version of the Orders API section. Using one and not
            # the other returns an error.
            orderstatus=('Unshipped', 'PartiallyShipped')
        ).parsed

        if not response.get('Orders'):
            return []

        # Orders are returned as dictionary for single order and as
        # list for multiple orders.
        # Convert to list if dictionary is returned
        if not isinstance(response['Orders']['Order'], list):
            orders = [response['Orders']['Order']]
        else:
            orders = response['Orders']['Order']

        while response.get('NextToken'):
            # Pull data from pagination
            response = order_api.list_orders_by_next_token(
                response['NextToken']['value']
            ).parsed

            if not isinstance(response['Orders']['Order'], list):
                new_orders = [response['Orders']['Order']]
            else:
                new_orders = response['Orders']['Order']

            orders.extend(new_orders)

        # Update last order import time for channel
        self.write([self], {'last_order_import_time': datetime.utcnow()})

        return self.import_order_bulk(
            [o['AmazonOrderId']['value'] for o in orders]
        )

    def import_order_bulk(self, amazon_order_ids):
        """
        It is expensive to get orders one by one and in addition, it will
        throttle the API requests.
        """
        Sale = Pool().get('sale.sale')

        sales = []
        order_api = self.get_amazon_order_api()

        for order_ids_batch in batch(amazon_order_ids, 50):
            # The order fetch API limits getting orders to a maximum
            # of 50 at a time
            try:
                orders_data = order_api.get_order(order_ids_batch).parsed
            except mws.MWSError, e:
                # Do not continue further in this method as further calls
                # to amazon will raise same error for further calls,
                # but calling return will let imported orders commit to
                # database. Else this will become a never ending process.
                logger.warning(e.message)
                return sales

            orders = orders_data['Orders']['Order']
            if not isinstance(orders, list):
                orders = [orders]

            for order in orders:
                already_imported = Sale.search([
                    ('channel', '=', self.id),
                    (
                        'channel_identifier', '=',
                        order['AmazonOrderId']['value']
                    ),
                ])
                if not already_imported:
                    # New order! get the line items and save the order.
                    order_line_data = order_api.list_order_items(
                        order['AmazonOrderId']['value']
                    ).parsed

                    with Transaction().set_context(
                        {'current_channel': self.id}
                    ):
                        sales.append(
                            Sale.create_using_amazon_data(
                                order,
                                order_line_data['OrderItems']['OrderItem']
                            )
                        )
                else:
                    # Order is already there, just ensure it is in the
                    # right status
                    already_imported[0].update_order_status_from_amazon_mws(
                        order
                    )
        return sales

    def import_order(self, order_id):
        """
        Downstream implementation of channel.import_order from sale channel

        WARNING: Using this API might result in you running out of Amazon
        API call credits. Use the bulk import API instead
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).import_order(order_id)

        Sale = Pool().get('sale.sale')

        sales = Sale.search([
            ('channel', '=', self.id),
            ('channel_identifier', '=', order_id),
        ])
        if sales:
            return sales[0]

        return self.import_order_bulk([order_id])[0]

    def _get_amazon_envelop(self, message_type, xml_list):
        """
        Returns amazon envelop for xml given
        """
        NS = "http://www.w3.org/2001/XMLSchema-instance"
        location_attribute = '{%s}noNamespaceSchemaLocation' % NS

        envelope_xml = E.AmazonEnvelope(
            E.Header(
                E.DocumentVersion('1.01'),
                E.MerchantIdentifier(self.amazon_merchant_id)
            ),
            E.MessageType(message_type),
            E.PurgeAndReplace('false'),
            *(xml for xml in xml_list)
        )
        envelope_xml.set(location_attribute, 'amznenvelope.xsd')

        return envelope_xml

    def export_product_catalog(self):
        """
        Export the products to the Amazon account in context
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).export_product_catalog()

        Product = Pool().get('product.product')

        self.validate_amazon_channel()

        domain = [
            ('template.export_to_amazon', '=', True),
            ('code', '!=', None),
            ('codes', 'not in', []),
        ]

        if self.last_product_export_time:
            domain.append(
                ('write_date', '>=', self.last_product_export_time)
            )

        products = Product.search(domain)

        products_xml = []
        for product in products:
            if not product.code:
                self.raise_user_error(
                    'missing_product_code', {
                        'product': product.name
                    }
                )
            if not product.codes:
                self.raise_user_error(
                    'missing_product_codes', {
                        'product': product.name
                    }
                )
            # Get the product's code to be set as standard ID to amazon
            product_standard_id = (
                product.asin or product.ean or product.upc or product.isbn
                or product.gtin
            )
            products_xml.append(E.Message(
                E.MessageID(str(product.id)),
                E.OperationType('Update'),
                E.Product(
                    E.SKU(product.code),
                    E.StandardProductID(
                        E.Type(product_standard_id.code_type.upper()),
                        E.Value(product_standard_id.code),
                    ),
                    E.DescriptionData(
                        E.Title(product.name),
                        E.Description(product.description),
                    ),
                    # Amazon needs this information so as to place the product
                    # under a category.
                    # FIXME: Either we need to create all that inside our
                    # system or figure out a way to get all that via API
                    E.ProductData(
                        E.Miscellaneous(
                            E.ProductType('Misc_Other'),
                        ),
                    ),
                )
            ))

        envelope_xml = self._get_amazon_envelop('Product', products_xml)

        feeds_api = self.get_amazon_feed_api()

        response = feeds_api.submit_feed(
            etree.tostring(envelope_xml),
            feed_type='_POST_PRODUCT_DATA_',
            marketplaceids=[self.amazon_marketplace_id]
        )

        # Update last product export time for channel
        self.write([self], {
            'last_product_export_time': datetime.utcnow()
        })

        Product.write(products, {
            'channel_listings': [
                ('create', [{
                    'channel': self.id,
                }])
            ]
        })

        return response.parsed

    def export_product_prices(self):
        """Export prices of the products to the Amazon account in context

        :param products: List of active records of products
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).export_product_prices()

        Product = Pool().get('product.product')

        products = Product.search([
            ('code', '!=', None),
            ('codes', 'not in', []),
            ('channel_listings.channel', '=', self.id),
        ])

        pricing_xml = []
        for product in products:
            if self in [
                ch.channel for ch in product.channel_listings
            ]:
                pricing_xml.append(E.Message(
                    E.MessageID(str(product.id)),
                    E.OperationType('Update'),
                    E.Price(
                        E.SKU(product.code),
                        E.StandardPrice(
                            # TODO: Use a pricelist
                            str(product.list_price),
                            currency=self.company.currency.code
                        ),
                    )
                ))

        envelope_xml = self._get_amazon_envelop('Price', pricing_xml)

        feeds_api = self.get_amazon_feed_api()

        feeds_api.submit_feed(
            etree.tostring(envelope_xml),
            feed_type='_POST_PRODUCT_PRICING_DATA_',
            marketplaceids=[self.amazon_marketplace_id]
        )

        return len(pricing_xml)

    def import_products(self):
        """
        Import all products for this amazon channel
        Downstream implementation for channel.import_products
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).import_products()

        report_api = self.get_mws_report_api()

        # Request report for active listings
        request_report = report_api.request_report(
            '_GET_MERCHANT_LISTINGS_DATA_LITER_'
        ).parsed

        # It takes 45 seconds for report to be generated
        time.sleep(45)

        request_list = report_api.get_report_request_list(
            [request_report['ReportRequestInfo']['ReportRequestId']['value']]
        ).parsed

        report = report_api.get_report(
            request_list['ReportRequestInfo']['GeneratedReportId']['value']
        ).parsed

        # Fetch skus from csv report file
        skus = [
            row['seller-sku'] for row in csv.DictReader(
                StringIO(report), delimiter='\t'
            )
        ]

        products = []
        for sku in skus:
            products.append(self.import_product(sku))

        return products

    def import_product(self, sku):
        """
        Import specific product for this amazon channel
        Downstream implementation for channel.import_product

        :param sku: Product Seller SKU from Amazon
        :returns: Active record of Product Created
        """
        Product = Pool().get('product.product')
        Listing = Pool().get('product.product.channel_listing')

        if self.source != 'amazon_mws':
            return super(SaleChannel, self).import_product(sku)

        # Check if there is a poduct with the seller SKU
        # The SKU on the product could be different from that of the
        # listing. Say older SKUs or Amazon assigned SKUs.
        exisiting_listings = Listing.search([
            ('product_identifier', '=', sku),
            ('channel', '=', self),
        ])
        if exisiting_listings:
            return exisiting_listings[0].product

        products = Product.search([('code', '=', sku)])
        product_api = self.get_amazon_product_api()
        if not products:
            # Create a product since there is no match for an existing
            # product with the SKU.

            product_data = product_api.get_matching_product_for_id(
                self.amazon_marketplace_id, 'SellerSKU', [sku]
            ).parsed

            products = [Product.create_from(self, product_data)]

        product, = products
        listings = Listing.search([
            ('product', '=', product),
            ('channel', '=', self),
        ])
        if not listings:
            product_data = product_api.get_matching_product_for_id(
                self.amazon_marketplace_id, 'SellerSKU', [sku]
            ).parsed
            Listing(
                product=product,
                channel=self,
                product_identifier=sku,
                asin=product_data['Products']['Product']['Identifiers']["MarketplaceASIN"]["ASIN"]["value"],  # noqa
            ).save()

        return product

    def import_order_states(self):
        """
        Import order states for amazon channel

        =================================================================
        |    OrderStatus   |                  Description               |
        =================================================================
        | Pending          |     Order has been placed but payment has  |
        |                  | not been authorized. Not ready for shipment|
        -----------------------------------------------------------------
        | Unshipped        |    Payment has been authorized and order is|
        |                  | ready for shipment, but no items in the    |
        |                  | order have been shipped                    |
        |----------------------------------------------------------------
        | PartiallyShipped |    One or more (but not all) items in the  |
        |                  | order have been shipped                    |
        |----------------------------------------------------------------
        | Shipped          |    All items in the order have been shipped|
        |----------------------------------------------------------------
        |InvoiceUnconfirmed|    All items in the order have been shipped|
        |                  | The seller has not yet given confirmation  |
        |                  | to Amazon that the invoice has been shipped|
        |                  | to the buyer. Note: This value is available|
        |                  | only in China                              |
        |----------------------------------------------------------------
        | Canceled         |    The order was canceled.                 |
        |----------------------------------------------------------------
        | Unfulfillable    |    The order cannot be fulfiled. This state|
        |                  | applies to only amazon-fulfiled orders that|
        |                  | were not placed on amazon's retail web site|
        |----------------------------------------------------------------

        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).import_order_states()

        order_states_data = [
            'Pending',
            'Unshipped',
            'PartiallyShipped',
            'Shipped',
            'InvoiceUnconfirmed',
            'Canceled',
            'Unfulfillable',
        ]

        with Transaction().set_context({'current_channel': self.id}):
            for name in order_states_data:
                self.create_order_state(name, name)

    def get_default_tryton_action(self, code, name):
        """
        Returns tryton order state for amazon order status
        """
        if self.source != 'amazon_mws':
            return super(SaleChannel, self).get_default_tryton_action(
                code, name)

        if code == 'PartiallyShipped':
            return {
                'action': 'process_manually',
                'invoice_method': 'shipment',
                'shipment_method': 'order'
            }
        elif code == 'Unshipped':
            return {
                'action': 'process_automatically',
                'invoice_method': 'shipment',
                'shipment_method': 'order'
            }
        elif code in (
            'Pending', 'Canceled', 'InvoiceUnconfirmed',
            'Unfulfillable',
        ):
            return {
                'action': 'do_not_import',
                'invoice_method': 'manual',
                'shipment_method': 'manual'
            }
        elif code == 'Shipped':
            return {
                'action': 'import_as_past',
                'invoice_method': 'order',
                'shipment_method': 'order'
            }

    def update_order_status(self):
        Sale = Pool().get('sale.sale')

        if self.source != 'amazon_mws':
            return super(SaleChannel, self).update_order_status()

        order_api = self.get_amazon_order_api()

        sales = Sale.search([
            ('channel', '=', self.id),
            ('state', 'in', ('confirmed', 'processing')),
        ])
        order_ids = [sale.channel_identifier for sale in sales]

        for order_ids_batch in batch(order_ids, 50):
            # The order fetch API limits getting orders to a maximum
            # of 50 at a time
            try:
                response = order_api.get_order(order_ids_batch).parsed
            except mws.MWSError, e:
                # Do not continue further in this method as further calls
                # to amazon will raise same error for further calls,
                # but calling return will let updated orders commit to
                # database. Else this will become a never ending process.
                logger.warning(e.message)
                return

            if not isinstance(response['Orders']['Order'], list):
                orders = [response['Orders']['Order']]
            else:
                orders = response['Orders']['Order']

            for order in orders:
                sale, = Sale.search([
                    ('channel_identifier', '=', order['AmazonOrderId']['value'])
                ])
                sale.update_order_status_from_amazon_mws(order)


class CheckAmazonServiceStatusView(ModelView):
    "Check Service Status View"
    __name__ = 'channel.check_amazon_service_status.view'

    status = fields.Char('Status', readonly=True)
    message = fields.Text("Message", readonly=True)


class CheckAmazonServiceStatus(Wizard):
    """
    Check Service Status Wizard

    Check service status for the current MWS account
    """
    __name__ = 'channel.check_amazon_service_status'

    start = StateView(
        'channel.check_amazon_service_status.view',
        'amazon_mws.check_amazon_service_status_view_form',
        [
            Button('OK', 'end', 'tryton-ok'),
        ]
    )

    def default_start(self, data):
        """
        Check the service status of the MWS account

        :param data: Wizard data
        """
        SaleChannel = Pool().get('sale.channel')

        channel = SaleChannel(Transaction().context.get('active_id'))

        res = {}
        api = channel.get_mws_api()
        response = api.get_service_status().parsed

        status = response['Status']['value']

        if status == 'GREEN':
            status_message = 'The service is operating normally. '

        elif status == 'GREEN_I':
            status_message = 'The service is operating normally. '

        elif status == 'YELLOW':
            status_message = 'The service is experiencing higher than ' + \
                'normal error rates or is operating with degraded performance. '
        else:
            status_message = 'The service is unavailable or experiencing ' + \
                'extremely high error rates. '

        res['status'] = status
        if not response.get('Messages'):
            res['message'] = status_message
            return res

        if isinstance(response['Messages']['Message'], dict):
            messages = [response['Messages']['Message']]
        else:
            messages = response['Messages']['Message']

        for message in messages:
            status_message = status_message + message['Text']['value'] + ' '
            res['message'] = status_message

        return res


class CheckAmazonSettingsView(ModelView):
    "Check Amazon Settings View"
    __name__ = 'channel.check_amazon_settings.view'

    status = fields.Text('Status', readonly=True)


class CheckAmazonSettings(Wizard):
    """
    Wizard to Check Amazon MWS Settings

    Check amazon settings configured for the current MWS account
    """
    __name__ = 'channel.check_amazon_settings'

    start = StateView(
        'channel.check_amazon_settings.view',
        'amazon_mws.check_amazon_settings_view_form',
        [
            Button('OK', 'end', 'tryton-ok'),
        ]
    )

    def default_start(self, data):
        """
        Check the amazon settings for the current account

        :param data: Wizard data
        """
        SaleChannel = Pool().get('sale.channel')

        channel = SaleChannel(Transaction().context.get('active_id'))

        channel.validate_amazon_channel()

        res = {}
        api = channel.get_amazon_feed_api()

        try:
            api.get_feed_submission_count().parsed
            res['status'] = 'Account settings have been configured correctly'

        except mws.MWSError:
            res['status'] = "Something went wrong. Please check account " + \
                "settings again"
        return res
