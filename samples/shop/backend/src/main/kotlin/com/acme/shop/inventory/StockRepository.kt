package com.acme.shop.inventory

import org.springframework.data.jpa.repository.JpaRepository

interface StockRepository : JpaRepository<StockItem, String> {
    fun findBySku(sku: String): StockItem?
}
