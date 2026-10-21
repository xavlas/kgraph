package com.acme.shop.inventory

import org.springframework.stereotype.Service

@Service
class InventoryService(private val stockRepository: StockRepository) {

    fun levels(): List<StockItem> = stockRepository.findAll()

    fun reserve(sku: String, qty: Int) {
        require(qty > 0) { "Reserved quantity must be positive" }
        val item = stockRepository.findBySku(sku) ?: throw IllegalStateException("Unknown SKU $sku")
        if (item.availableQty < qty) {
            throw IllegalStateException("Insufficient stock for $sku")
        }
        item.availableQty -= qty
        item.reserved += qty
        stockRepository.save(item)
    }
}
