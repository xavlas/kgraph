package com.acme.shop.inventory

import jakarta.persistence.Column
import jakarta.persistence.Entity
import jakarta.persistence.Id
import jakarta.persistence.Table

@Entity
@Table(name = "stock_items")
data class StockItem(
    @Id val sku: String,
    @Column(name = "available_qty") var availableQty: Int,
    var reserved: Int = 0
)
