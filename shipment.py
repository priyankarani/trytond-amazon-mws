# -*- coding: utf-8 -*-
"""
    shipment.py
"""
from collections import defaultdict
from lxml.builder import E
from lxml import etree
from trytond.pool import PoolMeta, Pool
from trytond.pyson import Eval
from trytond.model import fields, Workflow


__all__ = ['ShipmentOut', 'StockLocation', 'ShipmentInternal']
__metaclass__ = PoolMeta


class StockLocation:
    __name__ = 'stock.location'

    # This field is added so we can have sku of the product (fullfilled by
    # amazon) while sending product info to amazon network
    channel = fields.Many2One(
        "sale.channel", "Channel", states={
            'required': Eval('subtype') == 'fba',
            'invisible': Eval('subtype') != 'fba',
        }, domain=[('source', '=', 'amazon_mws')],
        depends=['subtype']
    )

    @classmethod
    def __setup__(cls):
        """
        Setup the class before adding to pool
        """
        super(StockLocation, cls).__setup__()

        fba = ('fba', 'Fullfilled By Amazon')

        if fba not in cls.subtype.selection:
            cls.subtype.selection.append(fba)


class ShipmentOut:
    "ShipmentOut"
    __name__ = 'stock.shipment.out'

    def export_shipment_status_to_amazon(self):
        """
        TODO: This should be done in bulk to avoid over using the amazon
        API.
        """
        SaleLine = Pool().get('sale.line')
        if self.state != 'done':
            return

        # Handle the case where a shipment could have been merged
        # across channels or even two amazon accounts.
        items_by_sale = defaultdict(list)

        # Find carrier code and shipment method
        fulfilment_elements = []
        carrier_code = None
        shipping_method = 'Standard'

        if self.carrier.carrier_cost_method in ('endicia', ):
            carrier_code = 'USPS'
            shipping_method = self.endicia_mailclass.name
        elif self.carrier.carrier_cost_method == 'fedex':
            carrier_code = 'FedEx'
            shipping_method = self.fedex_service_type.name
        elif self.carrier.carrier_cost_method == 'ups':
            carrier_code = 'UPS'
            shipping_method = self.ups_service_type.name
        # TODO: Add GLS etc

        if carrier_code is None:
            fulfilment_elements.append(
                E.CarrierName(
                    self.carrier and self.carrier.rec_name or 'self'
                )
            )
        else:
            fulfilment_elements.append(
                E.CarrierCode(carrier_code)
            )

        fulfilment_elements.extend([
            E.ShippingMethod(shipping_method),
            E.ShipperTrackingNumber(self.tracking_number),
        ])
        fulfilment_data = E.FulfillmentData(*fulfilment_elements)

        # For all outgoing moves add items
        for move in self.outgoing_moves:
            if not move.quantity:
                # back order
                continue
            if not isinstance(move.origin, SaleLine):
                continue
            if move.origin.sale.channel.source != 'amazon_mws':
                continue
            items_by_sale[move.origin.sale].append(
                E.Item(
                    E.AmazonOrderItemCode(move.origin.channel_identifier),
                    E.Quantity(str(int(move.quantity)))
                )
            )

        # For each sale, now export the data
        for sale, items in items_by_sale.items():
            message = E.Message(
                E.MessageID(str(sale.id)),  # just has to be unique in envelope
                E.OrderFulfillment(
                    E.AmazonOrderID(sale.channel_identifier),
                    E.FulfillmentDate(
                        self.write_date.strftime('%Y-%m-%dT00:00:00Z')
                    ),
                    fulfilment_data,
                    *items
                )
            )
            envelope_xml = sale.channel._get_amazon_envelop(
                'OrderFulfillment', [message]
            )
            feeds_api = sale.channel.get_amazon_feed_api()
            feeds_api.submit_feed(
                etree.tostring(envelope_xml),
                feed_type='_POST_ORDER_FULFILLMENT_DATA_',
                marketplaceids=[sale.channel.amazon_marketplace_id]
            )


class ShipmentInternal:
    "Internal Shipment"
    __name__ = 'stock.shipment.internal'

    @classmethod
    @Workflow.transition('assigned')
    def assign(cls, shipments):
        """
        Create inbound shipment for fba products
        """
        Listing = Pool().get('product.product.channel_listing')

        for shipment in shipments:
            to_warehouse = shipment.to_location.parent

            if to_warehouse.subtype != 'fba':
                continue

            channel = to_warehouse.channel

            channel.validate_amazon_channel()

            mws_connection_api = channel.get_mws_connection_api()

            from_address = shipment.from_location.parent.address

            if not from_address:
                cls.raise_user_error(
                    "Warehouse %s must have an address" % (
                        shipment.to_location.parent.title()
                    )
                )

            ship_from_address = from_address.to_fba()

            fba_moves = []
            for move in shipment.moves:
                listings = Listing.search([
                    ('product', '=', move.product.id),
                    ('channel', '=', channel.id)
                ], limit=1)
                if not listings:
                    cls.raise_user_error(
                        "Product %s is not listed on amazon" % (
                            move.product.rec_name
                        )
                    )
                listing, = listings
                if listing.fba_code:
                    fba_moves.append((listing.fba_code, move.quantity))

            request_items = dict(Member=[{
                'SellerSKU': sku,
                'Quantity': str(int(qty)),
            } for sku, qty in fba_moves])

            if not request_items:
                return

            # Create Inbond shipment plan, that would return info
            # required to create inbound shipment
            try:
                plan_response = mws_connection_api.create_inbound_shipment_plan(
                    ShipFromAddress=ship_from_address,
                    InboundShipmentPlanRequestItems=request_items
                )
            except Exception, e:  # XXX: Handle InvalidRequestException
                cls.raise_user_error(e.message)

            for plan in plan_response.CreateInboundShipmentPlanResult.InboundShipmentPlans:  # noqa
                shipment_header = {
                    'ShipmentName': '-'.join(
                        [shipment.rec_name, plan.ShipmentId]
                    ),
                    'ShipFromAddress': ship_from_address,
                    'DestinationFulfillmentCenterId':
                        plan.DestinationFulfillmentCenterId,
                    'LabelPrepPreference': plan.LabelPrepType,
                    'ShipmentStatus': 'WORKING',
                }
                shipment_items = dict(Member=[{
                    'SellerSKU': item.SellerSKU,
                    'QuantityShipped': item.Quantity,
                } for item in plan.Items])

                # Create inbound shipment for each item
                try:
                    mws_connection_api.create_inbound_shipment(
                        ShipmentId=plan.ShipmentId,
                        InboundShipmentHeader=shipment_header,
                        InboundShipmentItems=shipment_items
                    )
                except Exception, e:  # XXX: Handle InvalidRequestException
                    cls.raise_user_error(e.message)

        return super(ShipmentInternal, cls).assign(shipments)
