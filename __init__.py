# -*- coding: utf-8 -*-
"""
    __init__

    Initialize module

"""
from trytond.pool import Pool
from channel import (
    SaleChannel, CheckAmazonServiceStatus, CheckAmazonServiceStatusView,
    CheckAmazonSettingsView, CheckAmazonSettings
)
from product import (
    Product, ProductCode, Template,
    ProductSaleChannelListing
)
from sale import Sale
from party import Party, Address
from country import Subdivision
from shipment import ShipmentOut, StockLocation


def register():
    """
    Register classes with pool
    """
    Pool.register(
        SaleChannel,
        Product,
        ProductCode,
        Template,
        CheckAmazonServiceStatusView,
        CheckAmazonSettingsView,
        Sale,
        Party,
        Address,
        Subdivision,
        ProductSaleChannelListing,
        ShipmentOut,
        StockLocation,
        module='amazon_mws', type_='model'
    )
    Pool.register(
        CheckAmazonServiceStatus,
        CheckAmazonSettings,
        module='amazon_mws', type_='wizard'
    )
